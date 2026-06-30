"""Universal post-processing module framework — MK1.

Provides the shared contract, result object, context, validation, and chaining
behaviour that all future post-processing modules will use.

Future universal conveyor:

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

This module is the reusable infrastructure.  It does NOT implement any of those
modules.  No video rendering, captioning, formatting, validation, metadata
writing, output-funnel registration, or AI/LLM calls are present here.

Module statuses:
    PASS    — module completed successfully and produced the expected result.
    FAIL    — module attempted or was required but failed.
    SKIPPED — module was intentionally not run.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

MODULE_STATUS_PASS = "PASS"
MODULE_STATUS_FAIL = "FAIL"
MODULE_STATUS_SKIPPED = "SKIPPED"

VALID_MODULE_STATUSES = frozenset([MODULE_STATUS_PASS, MODULE_STATUS_FAIL, MODULE_STATUS_SKIPPED])

CHAIN_STATUS_PASS = "PASS"
CHAIN_STATUS_FAIL = "FAIL"

# ---------------------------------------------------------------------------
# Schema version constants
# ---------------------------------------------------------------------------

MODULE_RESULT_SCHEMA_VERSION = "post_processing_module_result_v1"
MODULE_CHAIN_RESULT_SCHEMA_VERSION = "post_processing_module_chain_result_v1"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ModuleResultValidationError(ValueError):
    """Raised when a module result dict does not match the required contract."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Module result helpers
# ---------------------------------------------------------------------------


def make_module_pass_result(
    module_name: str,
    module_version: str,
    *,
    input_path: str | None = None,
    output_path: str | None = None,
    config: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a PASS module result dict."""
    return {
        "schema_version": MODULE_RESULT_SCHEMA_VERSION,
        "module_name": module_name,
        "module_version": module_version,
        "status": MODULE_STATUS_PASS,
        "input_path": input_path,
        "output_path": output_path,
        "config": dict(config or {}),
        "error_reason": None,
        "warnings": list(warnings or []),
        "metadata": dict(metadata or {}),
    }


def make_module_fail_result(
    module_name: str,
    module_version: str,
    error_reason: str,
    *,
    input_path: str | None = None,
    output_path: str | None = None,
    config: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a FAIL module result dict.

    ``error_reason`` is required for FAIL results and should be a concise
    human-readable string describing what went wrong.
    """
    return {
        "schema_version": MODULE_RESULT_SCHEMA_VERSION,
        "module_name": module_name,
        "module_version": module_version,
        "status": MODULE_STATUS_FAIL,
        "input_path": input_path,
        "output_path": output_path,
        "config": dict(config or {}),
        "error_reason": error_reason,
        "warnings": list(warnings or []),
        "metadata": dict(metadata or {}),
    }


def make_module_skipped_result(
    module_name: str,
    module_version: str,
    *,
    reason: str | None = None,
    config: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a SKIPPED module result dict.

    ``reason`` is stored in ``metadata["skip_reason"]`` when provided.
    """
    extra_metadata: dict[str, Any] = dict(metadata or {})
    if reason is not None:
        extra_metadata["skip_reason"] = reason

    return {
        "schema_version": MODULE_RESULT_SCHEMA_VERSION,
        "module_name": module_name,
        "module_version": module_version,
        "status": MODULE_STATUS_SKIPPED,
        "input_path": None,
        "output_path": None,
        "config": dict(config or {}),
        "error_reason": None,
        "warnings": list(warnings or []),
        "metadata": extra_metadata,
    }


# ---------------------------------------------------------------------------
# Result validation
# ---------------------------------------------------------------------------

_REQUIRED_RESULT_FIELDS = (
    "schema_version",
    "module_name",
    "module_version",
    "status",
    "input_path",
    "output_path",
    "config",
    "error_reason",
    "warnings",
    "metadata",
)


def validate_module_result(result: Any) -> None:
    """Validate a module result dict against the standard contract.

    Raises :class:`ModuleResultValidationError` if the result is invalid.
    Does not return a value on success.
    """
    errors: list[str] = []

    if not isinstance(result, dict):
        raise ModuleResultValidationError(
            f"module result must be a dict, got {type(result).__name__}"
        )

    for field in _REQUIRED_RESULT_FIELDS:
        if field not in result:
            errors.append(f"{field!r} is required")

    if errors:
        raise ModuleResultValidationError(
            "invalid module result: " + "; ".join(errors)
        )

    status = result.get("status")
    if status not in VALID_MODULE_STATUSES:
        errors.append(
            f"status {status!r} is not valid; expected one of "
            f"{sorted(VALID_MODULE_STATUSES)}"
        )

    if result.get("schema_version") != MODULE_RESULT_SCHEMA_VERSION:
        errors.append(
            f"schema_version must equal {MODULE_RESULT_SCHEMA_VERSION!r}"
        )

    if not isinstance(result.get("module_name"), str) or not result["module_name"].strip():
        errors.append("module_name must be a non-empty string")

    if not isinstance(result.get("module_version"), str) or not result["module_version"].strip():
        errors.append("module_version must be a non-empty string")

    if not isinstance(result.get("config"), dict):
        errors.append("config must be a dict")

    if not isinstance(result.get("warnings"), list):
        errors.append("warnings must be a list")

    if not isinstance(result.get("metadata"), dict):
        errors.append("metadata must be a dict")

    if result.get("status") == MODULE_STATUS_FAIL and not result.get("error_reason"):
        errors.append("error_reason must be set for FAIL results")

    if errors:
        raise ModuleResultValidationError(
            "invalid module result: " + "; ".join(errors)
        )


# ---------------------------------------------------------------------------
# Module context
# ---------------------------------------------------------------------------


def make_module_context(
    *,
    job_id: str,
    candidate_id: str | None = None,
    source_video_path: str | None = None,
    working_dir: str | None = None,
    clip_dir: str | None = None,
    metadata_dir: str | None = None,
    tmp_dir: str | None = None,
    config: dict[str, Any] | None = None,
    selection_result: dict[str, Any] | None = None,
    selected_candidate: dict[str, Any] | None = None,
    module_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a module context dict.

    The context carries the information each module needs to do its work,
    without depending on global state, AI service state, transcript data,
    or output-funnel state.

    Args:
        job_id: Unique identifier for the current job.
        candidate_id: ID of the candidate being processed.
        source_video_path: Path to the original source video file.
        working_dir: Root working directory for post-processing artifacts.
        clip_dir: Directory where rendered clip files will be written.
        metadata_dir: Directory where per-clip metadata files will be written.
        tmp_dir: Temporary working directory for intermediate files.
        config: Module-level configuration overrides.
        selection_result: Full selection gate result from Prompt 15.
        selected_candidate: The single selected candidate this chain run is for.
        module_results: Accumulated results from modules already run.

    Returns:
        A plain dict context — no global state, no AI service references.
    """
    return {
        "job_id": job_id,
        "candidate_id": candidate_id,
        "source_video_path": source_video_path,
        "working_dir": working_dir,
        "clip_dir": clip_dir,
        "metadata_dir": metadata_dir,
        "tmp_dir": tmp_dir,
        "config": dict(config or {}),
        "selection_result": dict(selection_result or {}),
        "selected_candidate": dict(selected_candidate or {}),
        "module_results": list(module_results or []),
    }


# ---------------------------------------------------------------------------
# Base module class
# ---------------------------------------------------------------------------


class PostProcessingModule:
    """Base class for all MK1 post-processing modules.

    Subclasses must:
    - Set ``module_name`` as a non-empty class attribute.
    - Set ``module_version`` as a non-empty class attribute.
    - Implement :meth:`run` to return a module result dict built with
      :func:`make_module_pass_result`, :func:`make_module_fail_result`, or
      :func:`make_module_skipped_result`.

    The :meth:`run` signature is deliberately simple so that modules remain
    easy to test in isolation with no real video files, AI services, or
    output-funnel connections.
    """

    module_name: str = "unnamed_module"
    module_version: str = "1.0"

    def run(
        self,
        context: dict[str, Any],
        *,
        input_path: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute this module.

        Args:
            context: Module context dict from :func:`make_module_context`.
            input_path: Path to the file this module should consume.  For the
                first module in a chain this is the rendered clip.  For later
                modules it is the output of the previous module.
            config: Per-invocation config overrides (merged with context config
                by the implementation as needed).

        Returns:
            Module result dict.  Must be built with one of the
            ``make_module_*_result`` helpers.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.run() is not implemented"
        )


# ---------------------------------------------------------------------------
# Module chain helper
# ---------------------------------------------------------------------------


def run_module_chain(
    modules: list[Any],
    context: dict[str, Any],
    *,
    initial_input_path: str | None = None,
    allow_skipped: bool = False,
) -> dict[str, Any]:
    """Run a list of post-processing modules in order.

    Each module receives the ``output_path`` of the previous passing module as
    its ``input_path``.  If a module fails or (by default) is skipped, the
    chain stops immediately and returns a FAIL chain result.

    Expected module exceptions are caught and converted to controlled FAIL
    module results rather than being allowed to propagate as unhandled
    tracebacks.

    Args:
        modules: Ordered list of :class:`PostProcessingModule` instances **or**
            plain callables with signature
            ``(context, *, input_path, config) -> dict``.
        context: Module context dict from :func:`make_module_context`.  The
            original context dict is never mutated.
        initial_input_path: Path passed to the first module as its
            ``input_path``.
        allow_skipped: If ``True``, SKIPPED results are treated as
            non-blocking and the chain continues.  If ``False`` (default),
            a SKIPPED result from any required module fails the chain.

    Returns:
        Chain result dict with ``status`` equal to ``CHAIN_STATUS_PASS`` or
        ``CHAIN_STATUS_FAIL``.
    """
    accumulated_results: list[dict[str, Any]] = []
    accumulated_warnings: list[str] = []
    current_input_path: str | None = initial_input_path
    final_output_path: str | None = None

    for module in modules:
        module_name = _resolve_module_name(module)
        module_version = _resolve_module_version(module)

        # -- Run the module with prior module results, without mutating input context --
        invocation_context = dict(context)
        invocation_context["module_results"] = list(accumulated_results)
        try:
            result = _invoke_module(module, invocation_context, input_path=current_input_path)
        except Exception as exc:
            result = make_module_fail_result(
                module_name,
                module_version,
                error_reason=f"{type(exc).__name__}: {exc}",
                input_path=current_input_path,
            )

        # -- Validate the returned result shape --
        try:
            validate_module_result(result)
        except ModuleResultValidationError as exc:
            result = make_module_fail_result(
                module_name,
                module_version,
                error_reason=f"invalid_module_result: {exc.message}",
                input_path=current_input_path,
            )

        accumulated_results.append(result)
        accumulated_warnings.extend(result.get("warnings") or [])
        status = result.get("status")

        # -- Handle FAIL --
        if status == MODULE_STATUS_FAIL:
            return _chain_fail_result(
                module_results=accumulated_results,
                failed_module=module_name,
                error_reason=result.get("error_reason") or "module_failed",
                warnings=accumulated_warnings,
            )

        # -- Handle SKIPPED --
        if status == MODULE_STATUS_SKIPPED:
            if not allow_skipped:
                return _chain_fail_result(
                    module_results=accumulated_results,
                    failed_module=module_name,
                    error_reason="required_module_skipped",
                    warnings=accumulated_warnings,
                )
            # Allowed skip: do not update current_input_path
            continue

        # -- PASS: advance the path --
        if result.get("output_path") is not None:
            current_input_path = result["output_path"]
            final_output_path = result["output_path"]

    return {
        "schema_version": MODULE_CHAIN_RESULT_SCHEMA_VERSION,
        "status": CHAIN_STATUS_PASS,
        "final_output_path": final_output_path,
        "module_results": accumulated_results,
        "failed_module": None,
        "warnings": accumulated_warnings,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _invoke_module(
    module: Any,
    context: dict[str, Any],
    *,
    input_path: str | None,
) -> dict[str, Any]:
    """Dispatch module invocation for both class and function modules."""
    if isinstance(module, PostProcessingModule):
        return module.run(context, input_path=input_path)
    if callable(module):
        return module(context, input_path=input_path)
    raise TypeError(
        f"module must be a PostProcessingModule instance or callable, "
        f"got {type(module).__name__}"
    )


def _resolve_module_name(module: Any) -> str:
    if isinstance(module, PostProcessingModule):
        return module.module_name
    if hasattr(module, "module_name"):
        return str(module.module_name)
    if hasattr(module, "__name__"):
        return module.__name__
    return "unknown_module"


def _resolve_module_version(module: Any) -> str:
    if isinstance(module, PostProcessingModule):
        return module.module_version
    if hasattr(module, "module_version"):
        return str(module.module_version)
    return "1.0"


def _chain_fail_result(
    *,
    module_results: list[dict[str, Any]],
    failed_module: str,
    error_reason: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Build a FAIL chain result."""
    return {
        "schema_version": MODULE_CHAIN_RESULT_SCHEMA_VERSION,
        "status": CHAIN_STATUS_FAIL,
        "final_output_path": None,
        "module_results": module_results,
        "failed_module": failed_module,
        "warnings": warnings,
        "errors": [
            {
                "failed_module": failed_module,
                "reason": error_reason,
            }
        ],
    }
