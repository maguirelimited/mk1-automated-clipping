"""MK1 Evaluation stage — Selection Gate v1 baseline.

This module is the canonical MK1 **Evaluation** stage. It consumes processed
candidates from ``raw_candidate_pool.json`` (assembled after Candidate
Processing) and decides which candidates become rendered clips.

Evaluation produces ``selection_result.json`` with selected, reserve, and
rejected entries. It does not discover candidates, run boundary sanity, dedupe
overlaps, render media, or call AI.

**Current execution location:** Evaluation runs at the start of MK1
post-processing (``post_processing_mk1.py``), before the conveyor/render step.
That is the implementation location today, not a conceptual post-render stage.

**Scoring note:** Ranking uses deterministic thresholds and lexicographic sort
over existing Discovery-provided score fields. This is an operational Evaluation
baseline, not a statistically validated performance model. Do not treat it as
evidence-based weighted quality scoring.

Supported selection modes:
    maximum_quality         — strictest thresholds, fewest clips
    balanced                — default; high quality with useful volume
    growth                  — more exploration than balanced
    maximum_data_collection — lowest thresholds, most candidates
    custom                  — caller-provided thresholds

This module deliberately does NOT:
- render clips or modify video files
- call AI/LLM services
- rediscover transcript sections or candidates
- perform Candidate Processing (boundary sanity, overlap/dedupe)
- run post-processing upgrade modules (captions, B-roll, zoom, etc.)
- write post_processing_report.json
- register output funnels
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import processing_contracts as contracts
from processing_contracts import (
    REQUIRED_SCORE_FIELDS,
    ProcessingContractValidationError,
)

SELECTION_GATE_SCHEMA_VERSION = "selection_gate_v1"
SELECTION_RESULT_FILENAME = "selection_result.json"
MK1_EVALUATION_STRATEGY = "mk1_selection_gate_evaluation_v1"

STATUS_SELECTION_COMPLETE = "SELECTION_COMPLETE"
STATUS_SELECTION_FAILED = "SELECTION_FAILED"

DEFAULT_SELECTION_MODE = "balanced"

SUPPORTED_SELECTION_MODES = frozenset(
    [
        "maximum_quality",
        "balanced",
        "growth",
        "maximum_data_collection",
        "custom",
    ]
)

# ---------------------------------------------------------------------------
# Per-mode default configurations
# ---------------------------------------------------------------------------

_MODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "maximum_quality": {
        "max_clips": 3,
        "reserve_count": 2,
        "min_overall_potential": 8.5,
        "min_confidence": 0.75,
        "min_duration_sec": 20.0,
        "max_duration_sec": 90.0,
        "respect_candidate_warnings": True,
        "respect_transcript_quality_flags": True,
        "blocking_warnings": [],
        "blocking_transcript_quality_flags": [
            "low_transcript_confidence",
            "missing_words",
        ],
        "allow_reserve_candidates": True,
    },
    "balanced": {
        "max_clips": 6,
        "reserve_count": 3,
        "min_overall_potential": 7.0,
        "min_confidence": 0.6,
        "min_duration_sec": 15.0,
        "max_duration_sec": 120.0,
        "respect_candidate_warnings": True,
        "respect_transcript_quality_flags": True,
        "blocking_warnings": [],
        "blocking_transcript_quality_flags": [],
        "allow_reserve_candidates": True,
    },
    "growth": {
        "max_clips": 8,
        "reserve_count": 4,
        "min_overall_potential": 6.0,
        "min_confidence": 0.5,
        "min_duration_sec": 12.0,
        "max_duration_sec": 150.0,
        "respect_candidate_warnings": True,
        "respect_transcript_quality_flags": False,
        "blocking_warnings": [],
        "blocking_transcript_quality_flags": [],
        "allow_reserve_candidates": True,
    },
    "maximum_data_collection": {
        "max_clips": 10,
        "reserve_count": 5,
        "min_overall_potential": 5.0,
        "min_confidence": 0.4,
        "min_duration_sec": 10.0,
        "max_duration_sec": 180.0,
        "respect_candidate_warnings": False,
        "respect_transcript_quality_flags": False,
        "blocking_warnings": [],
        "blocking_transcript_quality_flags": [],
        "allow_reserve_candidates": True,
    },
    "custom": {
        # Safe defaults; callers should override all thresholds explicitly.
        "max_clips": 6,
        "reserve_count": 3,
        "min_overall_potential": 7.0,
        "min_confidence": 0.6,
        "min_duration_sec": 15.0,
        "max_duration_sec": 120.0,
        "respect_candidate_warnings": True,
        "respect_transcript_quality_flags": True,
        "blocking_warnings": [],
        "blocking_transcript_quality_flags": [],
        "allow_reserve_candidates": True,
    },
}


class SelectionGateError(RuntimeError):
    """Raised for cleanly classified selection gate failures.

    Use ``code`` to programmatically identify the failure category and
    ``message`` for a human-readable description.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_selection_gate_v1(
    raw_candidate_pool: dict[str, Any],
    *,
    job_metadata: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    selection_dir: str | None = None,
) -> dict[str, Any]:
    """Run MK1 Evaluation (Selection Gate v1) on a loaded raw candidate pool dict.

    Deterministically classifies each processed candidate as selected, reserve,
    or rejected, then returns a structured Evaluation result.

    Args:
        raw_candidate_pool: Validated ``raw_candidate_pool.json`` payload containing
            canonical ``mk1_candidate_v1`` candidates from Candidate Processing.
        job_metadata: Optional job-level metadata.  ``job_id`` is resolved
            from here first, then from the pool.
        config: Optional config overrides merged over mode defaults.
            ``config["selection_mode"]`` controls which mode is used.
        selection_dir: If provided, ``selection_result.json`` is written
            there after a successful run.

    Returns:
        Structured Evaluation result dict with status
        ``SELECTION_COMPLETE`` or ``SELECTION_FAILED``.
    """
    job_metadata = dict(job_metadata or {})
    config = dict(config or {})

    # -- Resolve job_id --
    job_id: str | None = job_metadata.get("job_id") or (
        raw_candidate_pool.get("job_id")
        if isinstance(raw_candidate_pool, dict)
        else None
    )

    # -- Validate pool shape --
    if not isinstance(raw_candidate_pool, dict):
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=config.get("selection_mode", DEFAULT_SELECTION_MODE),
            error_code="invalid_pool",
            error_message="raw_candidate_pool must be a dict",
        )

    # -- Resolve and validate selection mode --
    selection_mode: str = config.get("selection_mode", DEFAULT_SELECTION_MODE)
    if selection_mode not in SUPPORTED_SELECTION_MODES:
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=selection_mode,
            error_code="invalid_selection_mode",
            error_message=(
                f"selection_mode {selection_mode!r} is not supported; "
                f"expected one of {sorted(SUPPORTED_SELECTION_MODES)}"
            ),
        )

    # -- Merge config over mode defaults and validate --
    effective_config, config_error = _resolve_effective_config(selection_mode, config)
    if config_error is not None:
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=selection_mode,
            **config_error,
        )

    # -- Extract candidates list --
    candidates: Any = raw_candidate_pool.get("candidates", [])
    if not isinstance(candidates, list):
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=selection_mode,
            error_code="invalid_pool",
            error_message="raw_candidate_pool.candidates must be a list",
        )

    raw_count = len(candidates)
    gate_warnings: list[str] = []
    if raw_count == 0:
        gate_warnings.append("zero_candidates_received")

    # -- Classify each candidate --
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for candidate in candidates:
        reasons = _collect_rejection_reasons(candidate, effective_config, seen_ids)
        if reasons:
            rejected.append(_build_rejected_entry(candidate, reasons))
        else:
            eligible.append(candidate)
            cand_id = candidate.get("candidate_id") if isinstance(candidate, dict) else None
            if cand_id:
                seen_ids.add(cand_id)

    # -- Rank eligible candidates deterministically --
    ranked = sorted(eligible, key=_ranking_key)

    # -- Split into selected and reserve --
    max_clips: int = effective_config["max_clips"]
    reserve_limit: int = effective_config["reserve_count"]
    allow_reserve: bool = effective_config["allow_reserve_candidates"]

    selected_raw = ranked[:max_clips]
    leftover = ranked[max_clips:]

    selected: list[dict[str, Any]] = []
    for rank_idx, cand in enumerate(selected_raw, start=1):
        selected.append(_build_selected_entry(cand, rank=rank_idx))

    reserve: list[dict[str, Any]] = []
    if allow_reserve:
        for rank_idx, cand in enumerate(leftover[:reserve_limit], start=max_clips + 1):
            reserve.append(_build_reserve_entry(cand, rank=rank_idx))

    # -- Build summary --
    summary: dict[str, int] = {
        "raw_candidates_received": raw_count,
        "eligible_count": len(eligible),
        "selected_count": len(selected),
        "rejected_count": len(rejected),
        "reserve_count": len(reserve),
    }

    result: dict[str, Any] = {
        "schema_version": SELECTION_GATE_SCHEMA_VERSION,
        "job_id": job_id,
        "selection_mode": selection_mode,
        "status": STATUS_SELECTION_COMPLETE,
        "evaluation": _build_evaluation_metadata(
            selection_mode=selection_mode,
            input_candidate_count=raw_count,
            selected_count=len(selected),
            reserve_count=len(reserve),
            rejected_count=len(rejected),
        ),
        "selected_candidates": selected,
        "rejected_candidates": rejected,
        "reserve_candidates": reserve,
        "selection_summary": summary,
        "config_used": effective_config,
        "warnings": gate_warnings,
        "errors": [],
    }

    # -- Optionally persist selection artifact --
    if selection_dir is not None:
        _write_selection_result(result, selection_dir)

    return result


def run_selection_gate_v1_from_path(
    raw_candidate_pool_path: str,
    *,
    job_metadata: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    selection_dir: str | None = None,
) -> dict[str, Any]:
    """Load a raw candidate pool from disk and run Selection Gate v1.

    Returns ``SELECTION_FAILED`` if the pool cannot be loaded or validated,
    otherwise delegates to :func:`run_selection_gate_v1`.

    Args:
        raw_candidate_pool_path: Path to ``raw_candidate_pool.json``.
        job_metadata: Optional job-level metadata.
        config: Optional selection config overrides.
        selection_dir: If provided, ``selection_result.json`` is written there.

    Returns:
        Structured selection result dict.
    """
    job_metadata = dict(job_metadata or {})
    config = dict(config or {})
    selection_mode = config.get("selection_mode", DEFAULT_SELECTION_MODE)
    job_id: str | None = job_metadata.get("job_id")

    if not os.path.exists(raw_candidate_pool_path):
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=selection_mode,
            error_code="missing_raw_candidate_pool",
            error_message=(
                f"raw_candidate_pool.json not found: {raw_candidate_pool_path}"
            ),
        )

    if not os.path.isfile(raw_candidate_pool_path):
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=selection_mode,
            error_code="invalid_raw_candidate_pool_path",
            error_message=(
                f"raw_candidate_pool path is not a file: {raw_candidate_pool_path}"
            ),
        )

    try:
        pool_data: Any = json.loads(Path(raw_candidate_pool_path).read_bytes())
    except json.JSONDecodeError as exc:
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=selection_mode,
            error_code="invalid_raw_candidate_pool_json",
            error_message=f"raw_candidate_pool.json contains invalid JSON: {exc}",
        )
    except OSError as exc:
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=selection_mode,
            error_code="raw_candidate_pool_read_error",
            error_message=f"Could not read raw_candidate_pool.json: {exc}",
        )

    try:
        contracts.validate_raw_candidate_pool(pool_data)
    except ProcessingContractValidationError as exc:
        return _selection_failed_result(
            job_id=job_id,
            selection_mode=selection_mode,
            error_code="invalid_raw_candidate_pool_schema",
            error_message=str(exc),
        )

    return run_selection_gate_v1(
        pool_data,
        job_metadata=job_metadata,
        config=config,
        selection_dir=selection_dir,
    )


# ---------------------------------------------------------------------------
# Config resolution and validation
# ---------------------------------------------------------------------------


def _resolve_effective_config(
    selection_mode: str,
    user_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Merge user config over mode defaults and validate critical fields.

    Returns ``(effective_config, None)`` on success, or
    ``({}, error_dict)`` on validation failure.
    """
    base = dict(_MODE_DEFAULTS.get(selection_mode, _MODE_DEFAULTS["balanced"]))
    for key, value in user_config.items():
        if key == "selection_mode":
            continue
        base[key] = value

    errors: list[str] = []

    max_clips = base.get("max_clips")
    if not isinstance(max_clips, int) or isinstance(max_clips, bool) or max_clips < 1:
        errors.append("max_clips must be a positive integer")

    reserve_count = base.get("reserve_count")
    if (
        not isinstance(reserve_count, int)
        or isinstance(reserve_count, bool)
        or reserve_count < 0
    ):
        errors.append("reserve_count must be a non-negative integer")

    min_pot = base.get("min_overall_potential")
    if not _is_finite_float(min_pot) or not 0.0 <= float(min_pot) <= 10.0:
        errors.append("min_overall_potential must be a number within 0-10")

    min_conf = base.get("min_confidence")
    if not _is_finite_float(min_conf) or not 0.0 <= float(min_conf) <= 1.0:
        errors.append("min_confidence must be a number within 0-1")

    min_dur = base.get("min_duration_sec")
    if not _is_finite_float(min_dur) or float(min_dur) < 0:
        errors.append("min_duration_sec must be a non-negative number")

    max_dur = base.get("max_duration_sec")
    if not _is_finite_float(max_dur) or float(max_dur) < 0:
        errors.append("max_duration_sec must be a non-negative number")

    if (
        _is_finite_float(min_dur)
        and _is_finite_float(max_dur)
        and float(max_dur) < float(min_dur)
    ):
        errors.append("max_duration_sec must be >= min_duration_sec")

    blocking_w = base.get("blocking_warnings")
    if not isinstance(blocking_w, list):
        errors.append("blocking_warnings must be a list")

    blocking_f = base.get("blocking_transcript_quality_flags")
    if not isinstance(blocking_f, list):
        errors.append("blocking_transcript_quality_flags must be a list")

    if errors:
        return {}, {
            "error_code": "invalid_selection_config",
            "error_message": "invalid_selection_config: " + "; ".join(errors),
        }

    return base, None


# ---------------------------------------------------------------------------
# Candidate classification helpers
# ---------------------------------------------------------------------------


def _collect_rejection_reasons(
    candidate: Any,
    config: dict[str, Any],
    seen_ids: set[str],
) -> list[str]:
    """Return a list of rejection reasons for a candidate, or [] if eligible."""
    if not isinstance(candidate, dict):
        return ["invalid_candidate_shape"]

    reasons: list[str] = []

    # -- candidate_id --
    cand_id = candidate.get("candidate_id")
    if not isinstance(cand_id, str) or not cand_id.strip():
        reasons.append("missing_candidate_id")
    elif cand_id in seen_ids:
        reasons.append("duplicate_candidate")

    # -- timestamps --
    start = candidate.get("start_sec")
    end = candidate.get("end_sec")
    start_ok = _is_finite_float(start)
    end_ok = _is_finite_float(end)
    if not start_ok or not end_ok:
        reasons.append("invalid_timestamp")
    elif float(end) <= float(start):
        reasons.append("invalid_timestamp")

    # -- duration thresholds --
    duration = candidate.get("duration_sec")
    if _is_finite_float(duration):
        dur = float(duration)
        min_dur = float(config.get("min_duration_sec", 0))
        max_dur = config.get("max_duration_sec")
        if dur < min_dur:
            reasons.append("duration_too_short")
        elif max_dur is not None and _is_finite_float(max_dur) and dur > float(max_dur):
            reasons.append("duration_too_long")
    elif "invalid_timestamp" not in reasons:
        # duration_sec itself is invalid (but timestamps may be fine)
        reasons.append("invalid_timestamp")

    # -- required scores --
    scores = candidate.get("scores")
    if not isinstance(scores, dict):
        reasons.append("missing_required_score")
    else:
        missing_fields = [f for f in REQUIRED_SCORE_FIELDS if f not in scores]
        if missing_fields:
            reasons.append("missing_required_score")
        else:
            overall = scores.get("overall_potential")
            if not _is_finite_float(overall):
                reasons.append("missing_required_score")
            elif float(overall) < float(config.get("min_overall_potential", 0)):
                reasons.append("below_quality_threshold")

    # -- confidence --
    confidence = candidate.get("confidence")
    if not _is_finite_float(confidence):
        reasons.append("low_confidence")
    elif float(confidence) < float(config.get("min_confidence", 0)):
        reasons.append("low_confidence")

    # -- blocking warnings --
    if config.get("respect_candidate_warnings", True):
        blocking_warnings: set[str] = set(config.get("blocking_warnings") or [])
        if blocking_warnings:
            cand_warnings = candidate.get("warnings") or []
            if isinstance(cand_warnings, list):
                for w in cand_warnings:
                    if w in blocking_warnings:
                        reasons.append("warning_too_strong")
                        break

    # -- blocking transcript quality flags --
    if config.get("respect_transcript_quality_flags", True):
        blocking_flags: set[str] = set(
            config.get("blocking_transcript_quality_flags") or []
        )
        if blocking_flags:
            cand_flags = candidate.get("transcript_quality_flags") or []
            if isinstance(cand_flags, list):
                for flag in cand_flags:
                    if flag in blocking_flags:
                        reasons.append("transcript_quality_too_risky")
                        break

    return reasons


# ---------------------------------------------------------------------------
# Ranking key
# ---------------------------------------------------------------------------


def _ranking_key(candidate: dict[str, Any]) -> tuple:
    """Return a deterministic sort key tuple for ranking eligible candidates.

    Python sorts ascending, so negate numeric fields that should rank higher
    with a larger value.

    Priority:
        1. overall_potential (desc)
        2. retention_potential (desc)
        3. hook_strength (desc)
        4. insight_value (desc)
        5. confidence (desc)
        6. standalone_context (desc)
        7. natural_ending (desc)
        8. fewer warnings (asc)
        9. fewer transcript_quality_flags (asc)
       10. earlier start_sec (asc)
       11. candidate_id lexicographic (asc) — stable final tie-break
    """
    scores = candidate.get("scores") or {}
    return (
        -float(scores.get("overall_potential", 0)),
        -float(scores.get("retention_potential", 0)),
        -float(scores.get("hook_strength", 0)),
        -float(scores.get("insight_value", 0)),
        -float(candidate.get("confidence", 0)),
        -float(scores.get("standalone_context", 0)),
        -float(scores.get("natural_ending", 0)),
        len(candidate.get("warnings") or []),
        len(candidate.get("transcript_quality_flags") or []),
        float(candidate.get("start_sec", 0)),
        str(candidate.get("candidate_id", "")),
    )


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


def _build_evaluation_metadata(
    *,
    selection_mode: str,
    input_candidate_count: int,
    selected_count: int,
    reserve_count: int,
    rejected_count: int,
) -> dict[str, Any]:
    return {
        "strategy": MK1_EVALUATION_STRATEGY,
        "mode": selection_mode,
        "input_candidate_count": input_candidate_count,
        "selected_count": selected_count,
        "reserve_count": reserve_count,
        "rejected_count": rejected_count,
    }


def _build_selected_entry(candidate: dict[str, Any], *, rank: int) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "rank": rank,
        "selection_reason": "selected_by_rank",
        "start_sec": candidate.get("start_sec"),
        "end_sec": candidate.get("end_sec"),
        "duration_sec": candidate.get("duration_sec"),
        "confidence": candidate.get("confidence"),
        "scores": candidate.get("scores"),
        "archetype": candidate.get("archetype"),
        "warnings": list(candidate.get("warnings") or []),
        "transcript_quality_flags": list(
            candidate.get("transcript_quality_flags") or []
        ),
        "source_candidate": candidate,
    }


def _build_reserve_entry(candidate: dict[str, Any], *, rank: int) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "rank": rank,
        "reserve_reason": "over_max_clip_count",
        "start_sec": candidate.get("start_sec"),
        "end_sec": candidate.get("end_sec"),
        "duration_sec": candidate.get("duration_sec"),
        "confidence": candidate.get("confidence"),
        "scores": candidate.get("scores"),
        "archetype": candidate.get("archetype"),
        "warnings": list(candidate.get("warnings") or []),
        "transcript_quality_flags": list(
            candidate.get("transcript_quality_flags") or []
        ),
        "source_candidate": candidate,
    }


def _build_rejected_entry(candidate: Any, reasons: list[str]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {
            "candidate_id": None,
            "rejection_reasons": reasons,
            "scores": {},
            "confidence": None,
            "start_sec": None,
            "end_sec": None,
        }
    return {
        "candidate_id": candidate.get("candidate_id"),
        "rejection_reasons": reasons,
        "scores": candidate.get("scores") or {},
        "confidence": candidate.get("confidence"),
        "start_sec": candidate.get("start_sec"),
        "end_sec": candidate.get("end_sec"),
    }


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------


def _write_selection_result(result: dict[str, Any], selection_dir: str) -> str:
    """Write selection_result.json to ``selection_dir``.

    Creates ``selection_dir`` if it does not exist.
    Returns the path to the written file.
    """
    os.makedirs(selection_dir, exist_ok=True)
    path = os.path.join(selection_dir, SELECTION_RESULT_FILENAME)
    Path(path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Failure result builder
# ---------------------------------------------------------------------------


def _selection_failed_result(
    *,
    job_id: str | None,
    selection_mode: str,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    """Build a structured SELECTION_FAILED result."""
    empty_summary: dict[str, int] = {
        "raw_candidates_received": 0,
        "eligible_count": 0,
        "selected_count": 0,
        "rejected_count": 0,
        "reserve_count": 0,
    }
    return {
        "schema_version": SELECTION_GATE_SCHEMA_VERSION,
        "job_id": job_id,
        "selection_mode": selection_mode,
        "status": STATUS_SELECTION_FAILED,
        "evaluation": _build_evaluation_metadata(
            selection_mode=selection_mode,
            input_candidate_count=0,
            selected_count=0,
            reserve_count=0,
            rejected_count=0,
        ),
        "selected_candidates": [],
        "rejected_candidates": [],
        "reserve_candidates": [],
        "selection_summary": empty_summary,
        "config_used": {},
        "warnings": [],
        "errors": [
            {
                "code": error_code,
                "message": error_message,
            }
        ],
    }


# Alias for MK1 staged architecture documentation. Behaviour identical to
# :func:`run_selection_gate_v1`.
run_mk1_evaluation = run_selection_gate_v1


# ---------------------------------------------------------------------------
# Numeric helper
# ---------------------------------------------------------------------------


def _is_finite_float(value: Any) -> bool:
    """Return True if value is a non-bool numeric finite number."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))
