"""metadata_writer_v1 — fifth and final module in the fixed MK1 universal conveyor.

Writes one deterministic per-clip metadata JSON file after all prior modules
have run.  It does not transform the clip in any way; the final clip path is
preserved as ``output_path`` so the conveyor chain sees the finished video.

This module deliberately does NOT:
- edit, move, re-encode, or delete the final clip
- write a job-level post_processing_report.json
- register the clip with an output funnel
- generate AI/LLM metadata (titles, descriptions, hashtags)
- perform database writes or analytics tracking
- implement upload/scheduling
- perform recursive quality loops, B-roll, face/object tracking, etc.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any

from post_processing_modules import (
    PostProcessingModule,
    make_module_fail_result,
    make_module_pass_result,
)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

MODULE_NAME = "metadata_writer_v1"
MODULE_VERSION = "1.0"
CLIP_METADATA_SCHEMA_VERSION = "clip_metadata_v1"

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "allow_overwrite": False,
    "indent": 2,
    "sort_keys": True,
}

# ---------------------------------------------------------------------------
# Public module class
# ---------------------------------------------------------------------------


class MetadataWriterV1Module(PostProcessingModule):
    """Real MK1 metadata writer module.

    Writes one per-clip JSON metadata file capturing selected-candidate
    evidence, all prior module results, validation outcome, and the final
    output video path.

    The module result's ``output_path`` is always set to ``input_path``
    (the finished clip) so the conveyor chain does not accidentally expose
    the metadata JSON as the final clip.

    The written metadata JSON path is recorded in:
    - ``result["metadata"]["metadata_path"]``
    - the JSON itself under the ``metadata_path`` key
    """

    module_name = MODULE_NAME
    module_version = MODULE_VERSION

    def run(
        self,
        context: dict[str, Any],
        *,
        input_path: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Merge config: defaults < direct arg < context sub-key
        ctx_config = context.get("config") or {}
        v1_ctx_config = ctx_config.get("metadata_writer_v1") or {}
        merged_config = {**_DEFAULT_CONFIG, **(config or {}), **v1_ctx_config}

        # Validate numeric/type config values early
        config_err = _validate_config(merged_config)
        if config_err:
            return _fail(
                "invalid_metadata_config",
                config_err,
                input_path=input_path,
                clip_id=None,
                candidate_id=None,
                metadata_path=None,
            )

        allow_overwrite = bool(merged_config.get("allow_overwrite", False))
        indent = int(merged_config.get("indent", 2))
        sort_keys = bool(merged_config.get("sort_keys", True))

        # ------------------------------------------------------------------
        # 1. Resolve IDs and candidate
        # ------------------------------------------------------------------
        job_id = _resolve_job_id(context)
        selected = dict(context.get("selected_candidate") or {})
        source_candidate = dict(context.get("source_candidate") or {})

        # candidate_id: prefer selected_candidate, fall back to source_candidate
        candidate_id = (
            str(selected.get("candidate_id") or "").strip()
            or str(source_candidate.get("candidate_id") or "").strip()
        )

        # clip_id: from context first, then build from job + candidate
        clip_id = str(context.get("clip_id") or "").strip()
        if not clip_id:
            if job_id and candidate_id:
                clip_id = f"{_safe_part(job_id)}_{_safe_part(candidate_id)}"
            elif job_id:
                clip_id = _safe_part(job_id)
            elif candidate_id:
                clip_id = _safe_part(candidate_id)

        if not clip_id:
            return _fail(
                "missing_clip_id",
                "could not resolve a clip_id from context (job_id and candidate_id both missing)",
                input_path=input_path,
                clip_id=None,
                candidate_id=candidate_id or None,
                metadata_path=None,
            )

        if not candidate_id:
            return _fail(
                "missing_candidate_id",
                "could not resolve a candidate_id from selected_candidate or source_candidate",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=None,
                metadata_path=None,
            )

        # ------------------------------------------------------------------
        # 2. Resolve metadata directory
        # ------------------------------------------------------------------
        metadata_dir = _resolve_metadata_dir(context, merged_config, input_path)
        if not metadata_dir:
            return _fail(
                "missing_metadata_dir",
                "could not resolve a metadata directory from context or config",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=candidate_id,
                metadata_path=None,
            )

        try:
            os.makedirs(metadata_dir, exist_ok=True)
        except OSError as exc:
            return _fail(
                "metadata_dir_create_failed",
                f"could not create metadata directory {metadata_dir!r}: {exc}",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=candidate_id,
                metadata_path=None,
            )

        # ------------------------------------------------------------------
        # 3. Resolve output (clip) path
        # ------------------------------------------------------------------
        final_clip_path: str | None = None
        if input_path and str(input_path).strip():
            final_clip_path = str(input_path)
        else:
            # Try to find the clip path from the last PASS module result
            module_results = list(context.get("module_results") or [])
            for r in reversed(module_results):
                if isinstance(r, dict) and r.get("output_path"):
                    final_clip_path = str(r["output_path"])
                    break

        # ------------------------------------------------------------------
        # 4. Determine metadata file path
        # ------------------------------------------------------------------
        safe_clip_id = _safe_part(clip_id)
        metadata_filename = f"{safe_clip_id}_metadata_writer_v1.json"
        metadata_path = os.path.join(metadata_dir, metadata_filename)

        if os.path.exists(metadata_path) and not allow_overwrite:
            return _fail(
                "metadata_file_exists",
                f"metadata file already exists and allow_overwrite=false: {metadata_path}",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=candidate_id,
                metadata_path=metadata_path,
            )

        # ------------------------------------------------------------------
        # 5. Gather module results and build validation summary
        # ------------------------------------------------------------------
        module_results: list[dict[str, Any]] = list(context.get("module_results") or [])
        validation_result, failure_reason, failed_module = _resolve_validation_info(
            module_results
        )

        warnings: list[str] = []
        if validation_result == "UNKNOWN":
            warnings.append("missing_validation_result")

        # ------------------------------------------------------------------
        # 6. Build candidate fields
        # ------------------------------------------------------------------
        candidate = selected if selected else source_candidate
        start_sec = _safe_float(candidate.get("start_sec"))
        end_sec = _safe_float(candidate.get("end_sec"))
        duration_sec = _safe_float(candidate.get("duration_sec"))
        if duration_sec is None and start_sec is not None and end_sec is not None:
            duration_sec = round(end_sec - start_sec, 6)

        # ------------------------------------------------------------------
        # 7. Build modules_applied / versions / configs
        # ------------------------------------------------------------------
        modules_applied, module_versions, module_configs = _summarise_modules(
            module_results, MODULE_NAME, MODULE_VERSION, merged_config
        )

        # ------------------------------------------------------------------
        # 8. Build normalised module result list for JSON
        # ------------------------------------------------------------------
        safe_module_results = _normalise_module_results(module_results)

        # ------------------------------------------------------------------
        # 9. Collect top-level warnings from all modules
        # ------------------------------------------------------------------
        for r in module_results:
            if isinstance(r, dict):
                for w in r.get("warnings") or []:
                    if w not in warnings:
                        warnings.append(w)

        # ------------------------------------------------------------------
        # 10. Compose clip metadata dict
        # ------------------------------------------------------------------
        now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

        clip_metadata: dict[str, Any] = {
            "schema_version": CLIP_METADATA_SCHEMA_VERSION,
            "metadata_writer_version": MODULE_VERSION,
            "clip_id": clip_id,
            "job_id": job_id,
            "source_candidate_id": candidate_id,
            "source_video_path": str(context.get("source_video_path") or "") or None,
            "output_file_path": final_clip_path,
            "metadata_path": metadata_path,
            # Candidate timing
            "input_start_sec": start_sec,
            "input_end_sec": end_sec,
            "input_duration_sec": duration_sec,
            # Candidate evidence
            "input_candidate_scores": _safe_dict(candidate.get("scores")),
            "input_candidate_archetype": _safe_str(candidate.get("archetype")),
            "input_candidate_confidence": _safe_float(candidate.get("confidence")),
            "input_candidate_hook_text": _safe_str(candidate.get("hook_text")),
            "input_candidate_core_idea_summary": _safe_str(candidate.get("core_idea_summary")),
            "input_candidate_warnings": _safe_list(candidate.get("warnings")),
            "input_candidate_transcript_quality_flags": _safe_list(
                candidate.get("transcript_quality_flags")
            ),
            # Selection provenance
            "selection_mode": _safe_str(
                context.get("selection_mode")
                or (context.get("selection_result") or {}).get("selection_mode")
            ),
            "selection_rank": candidate.get("rank"),
            "selection_reason": _safe_str(candidate.get("selection_reason")),
            # Module chain
            "modules_applied": modules_applied,
            "module_versions": module_versions,
            "module_configs": module_configs,
            "module_results": safe_module_results,
            # Validation outcome
            "validation_result": validation_result,
            "failure_reason": failure_reason,
            "failed_module": failed_module,
            "warnings": warnings,
            # Timestamp
            "created_at": now,
        }

        # ------------------------------------------------------------------
        # 11. Serialise and write
        # ------------------------------------------------------------------
        try:
            json_bytes = json.dumps(
                clip_metadata, indent=indent, sort_keys=sort_keys, ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return _fail(
                "metadata_json_invalid",
                f"metadata dict could not be serialised to JSON: {exc}",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=candidate_id,
                metadata_path=metadata_path,
            )

        try:
            with open(metadata_path, "wb") as fh:
                fh.write(json_bytes)
        except OSError as exc:
            return _fail(
                "metadata_write_failed",
                f"failed to write metadata file {metadata_path!r}: {exc}",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=candidate_id,
                metadata_path=metadata_path,
            )

        # ------------------------------------------------------------------
        # 12. Read-back validation
        # ------------------------------------------------------------------
        try:
            with open(metadata_path, "r", encoding="utf-8") as fh:
                readback = json.load(fh)
        except json.JSONDecodeError as exc:
            return _fail(
                "metadata_readback_failed",
                f"metadata file was written but is not valid JSON: {exc}",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=candidate_id,
                metadata_path=metadata_path,
            )
        except OSError as exc:
            return _fail(
                "metadata_readback_failed",
                f"metadata file was written but could not be read back: {exc}",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=candidate_id,
                metadata_path=metadata_path,
            )

        if not isinstance(readback, dict):
            return _fail(
                "metadata_json_not_object",
                f"metadata file reads back as {type(readback).__name__}, expected dict",
                input_path=input_path,
                clip_id=clip_id,
                candidate_id=candidate_id,
                metadata_path=metadata_path,
            )

        metadata_file_size = os.path.getsize(metadata_path)

        # ------------------------------------------------------------------
        # 13. Return PASS result — output_path = input_path (clip, not JSON)
        # ------------------------------------------------------------------
        return make_module_pass_result(
            MODULE_NAME,
            MODULE_VERSION,
            input_path=input_path,
            output_path=input_path,  # non-transforming: preserve clip path
            config=merged_config,
            warnings=warnings,
            metadata={
                "metadata_path": metadata_path,
                "schema_version": CLIP_METADATA_SCHEMA_VERSION,
                "clip_id": clip_id,
                "source_candidate_id": candidate_id,
                "output_file_path": final_clip_path,
                "validation_result": validation_result,
                "failed_module": failed_module,
                "failure_reason": failure_reason,
                "module_count": len(modules_applied),
                "metadata_file_size_bytes": metadata_file_size,
            },
        )


# ---------------------------------------------------------------------------
# Conveyor registry helpers
# ---------------------------------------------------------------------------

METADATA_WRITER_V1_MODULE = MetadataWriterV1Module()


def get_metadata_writer_v1_module() -> MetadataWriterV1Module:
    """Return a fresh MetadataWriterV1Module instance for the conveyor registry."""
    return MetadataWriterV1Module()


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _validate_config(config: dict[str, Any]) -> str | None:
    """Return an error string if config is invalid, else None."""
    indent = config.get("indent")
    if not isinstance(indent, int) or isinstance(indent, bool) or indent < 0 or indent > 8:
        return f"indent must be an integer 0-8, got {indent!r}"

    allow_overwrite = config.get("allow_overwrite")
    if not isinstance(allow_overwrite, bool):
        return f"allow_overwrite must be a bool, got {allow_overwrite!r}"

    sort_keys = config.get("sort_keys")
    if not isinstance(sort_keys, bool):
        return f"sort_keys must be a bool, got {sort_keys!r}"

    return None


# ---------------------------------------------------------------------------
# Directory resolution
# ---------------------------------------------------------------------------


def _resolve_metadata_dir(
    context: dict[str, Any],
    config: dict[str, Any],
    input_path: str | None,
) -> str | None:
    """Resolve the metadata directory from context/config in priority order."""
    # 1. context["metadata_dir"]
    d = context.get("metadata_dir")
    if d and str(d).strip():
        return str(d)

    # 2. context["post_processing_dirs"]["metadata"]
    ppd = context.get("post_processing_dirs")
    if isinstance(ppd, dict):
        d = ppd.get("metadata")
        if d and str(d).strip():
            return str(d)

    # 3. context["paths"]["metadata_dir"]
    paths = context.get("paths")
    if isinstance(paths, dict):
        d = paths.get("metadata_dir")
        if d and str(d).strip():
            return str(d)

    # 4. config["metadata_dir"]
    d = config.get("metadata_dir")
    if d and str(d).strip():
        return str(d)

    # 5. Fallback: sibling "metadata" directory next to input_path
    if input_path and str(input_path).strip():
        parent = os.path.dirname(os.path.abspath(str(input_path)))
        return os.path.join(parent, "metadata")

    return None


# ---------------------------------------------------------------------------
# Validation info resolution
# ---------------------------------------------------------------------------


def _resolve_validation_info(
    module_results: list[dict[str, Any]],
) -> tuple[str, str | None, str | None]:
    """Return (validation_result, failure_reason, failed_module).

    Priority:
    1. Look for a validation_v1 result.
    2. Look for any FAIL result from an earlier module.
    3. Return UNKNOWN if no validation result found.
    """
    validation_result_entry: dict[str, Any] | None = None
    first_fail: dict[str, Any] | None = None

    for r in module_results:
        if not isinstance(r, dict):
            continue
        if r.get("module_name") == "validation_v1":
            validation_result_entry = r
        if r.get("status") == "FAIL" and first_fail is None:
            first_fail = r

    if validation_result_entry is not None:
        status = validation_result_entry.get("status", "UNKNOWN")
        if status == "PASS":
            return ("PASS", None, None)
        if status == "FAIL":
            return (
                "FAIL",
                str(validation_result_entry.get("error_reason") or ""),
                "validation_v1",
            )
        return ("UNKNOWN", None, None)

    if first_fail is not None:
        return (
            "FAIL",
            str(first_fail.get("error_reason") or ""),
            str(first_fail.get("module_name") or "unknown"),
        )

    return ("UNKNOWN", None, None)


# ---------------------------------------------------------------------------
# Module summary helpers
# ---------------------------------------------------------------------------


def _summarise_modules(
    module_results: list[dict[str, Any]],
    current_name: str,
    current_version: str,
    current_config: dict[str, Any],
) -> tuple[list[str], dict[str, str], dict[str, dict]]:
    """Return (modules_applied, module_versions, module_configs) for metadata."""
    modules_applied: list[str] = []
    module_versions: dict[str, str] = {}
    module_configs: dict[str, dict] = {}

    seen: set[str] = set()
    for r in module_results:
        if not isinstance(r, dict):
            continue
        name = r.get("module_name")
        version = r.get("module_version")
        cfg = r.get("config")
        if isinstance(name, str) and name and name not in seen:
            modules_applied.append(name)
            seen.add(name)
        if isinstance(name, str) and name and isinstance(version, str):
            module_versions[name] = version
        if isinstance(name, str) and name and isinstance(cfg, dict):
            module_configs[name] = cfg

    # Include current module
    if current_name not in seen:
        modules_applied.append(current_name)
    module_versions[current_name] = current_version
    module_configs[current_name] = dict(current_config)

    return modules_applied, module_versions, module_configs


# ---------------------------------------------------------------------------
# Module result normalisation
# ---------------------------------------------------------------------------


def _normalise_module_results(
    module_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a JSON-safe list of normalised module result dicts."""
    normalised: list[dict[str, Any]] = []
    for r in module_results:
        if not isinstance(r, dict):
            continue
        try:
            # Round-trip through JSON to ensure serializability; drop
            # non-serialisable values rather than crashing.
            safe = _deep_json_safe(r)
            normalised.append(safe)
        except Exception:
            normalised.append({
                "module_name": r.get("module_name"),
                "module_version": r.get("module_version"),
                "status": r.get("status"),
                "error_reason": r.get("error_reason"),
                "serialisation_error": True,
            })
    return normalised


def _deep_json_safe(obj: Any) -> Any:
    """Recursively convert obj to a JSON-safe primitive."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _deep_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deep_json_safe(v) for v in obj]
    # Fallback: stringify
    return str(obj)


# ---------------------------------------------------------------------------
# Safe type coercion helpers
# ---------------------------------------------------------------------------


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): _deep_json_safe(v) for k, v in value.items()}
    return {}


def _safe_list(value: Any) -> list:
    if isinstance(value, list):
        return [_deep_json_safe(v) for v in value]
    return []


def _safe_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(value))


def _resolve_job_id(context: dict[str, Any]) -> str | None:
    jid = context.get("job_id")
    if jid and str(jid).strip():
        return str(jid).strip()
    meta = context.get("job_metadata") or {}
    if isinstance(meta, dict):
        jid = meta.get("job_id")
        if jid and str(jid).strip():
            return str(jid).strip()
    return None


# ---------------------------------------------------------------------------
# Failure result helper
# ---------------------------------------------------------------------------


def _fail(
    failure_code: str,
    message: str,
    *,
    input_path: str | None,
    clip_id: str | None,
    candidate_id: str | None,
    metadata_path: str | None,
) -> dict[str, Any]:
    """Build a standard FAIL module result for this module."""
    return make_module_fail_result(
        MODULE_NAME,
        MODULE_VERSION,
        failure_code,
        input_path=input_path,
        output_path=None,
        metadata={
            "failure_code": failure_code,
            "message": message,
            "clip_id": clip_id,
            "source_candidate_id": candidate_id,
            "metadata_path": metadata_path,
        },
    )
