"""Local AI / clip-selection configuration contract for the Ops UI.

This module owns the operator-facing definition of the local-AI settings the
UI can view and edit. The values are persisted by :class:`ControlStore` and
mirrored into the shared ``controls.json`` file (under an ``ai_config`` block)
so the deterministic services (``video-automation``, ``ai-service``) can read
them without calling the Ops UI over HTTP.

Design rules:
- Ops UI is the control plane. It writes the shared file; services read it.
- Saved values are stored as strings (like environment variables). Readers
  coerce them. Defaults here match the service-side defaults exactly so an
  unsaved field behaves identically to "no override".
- Resolution order for every consuming service is:
  per-run option (where applicable) -> UI saved value -> env var -> default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Keys inside the shared controls.json ``ai_config`` block use these bare names.
# The SQLite control rows namespace them with this prefix to avoid colliding
# with the boolean control-plane flags.
AI_CONFIG_STORE_PREFIX = "ai_config."
AI_CONFIG_FILE_KEY = "ai_config"

CLIP_SELECTION_BACKENDS = ("openai", "ai_service")


@dataclass(frozen=True)
class AiConfigField:
    name: str
    label: str
    kind: str  # "choice" | "text" | "int" | "float"
    default: Any
    env_var: str
    help: str
    choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None


AI_CONFIG_FIELDS: tuple[AiConfigField, ...] = (
    AiConfigField(
        name="clip_selection_backend",
        label="Clip selection backend",
        kind="choice",
        default="ai_service",
        env_var="CLIP_SELECTION_BACKEND",
        choices=CLIP_SELECTION_BACKENDS,
        help=(
            "ai_service = local ai-service via Ollama (default). "
            "openai = legacy cloud selector, kept for rollback/testing/benchmarking. "
            "When ai_service is selected there is no silent fallback to OpenAI."
        ),
    ),
    AiConfigField(
        name="ai_service_url",
        label="ai-service URL",
        kind="text",
        default="http://127.0.0.1:5075",
        env_var="AI_SERVICE_URL",
        help="Base URL video-automation uses to reach the local ai-service.",
    ),
    AiConfigField(
        name="ai_service_timeout_seconds",
        label="ai-service request timeout (s)",
        kind="float",
        default=180.0,
        env_var="AI_SERVICE_TIMEOUT_SECONDS",
        minimum=1.0,
        maximum=3600.0,
        help="Per-request timeout for the video-automation -> ai-service /ai/run call.",
    ),
    AiConfigField(
        name="ai_provider",
        label="Model provider",
        kind="text",
        default="ollama",
        env_var="AI_PROVIDER",
        help="Local model backend provider. MK1 ships the Ollama backend only.",
    ),
    AiConfigField(
        name="ai_model",
        label="Model",
        kind="text",
        default="qwen2.5:14b-instruct",
        env_var="AI_MODEL",
        help="Local model tag the ai-service loads (e.g. qwen2.5:14b-instruct).",
    ),
    AiConfigField(
        name="ai_base_url",
        label="Model backend URL",
        kind="text",
        default="http://localhost:11434",
        env_var="AI_BASE_URL",
        help="Ollama backend base URL the ai-service talks to.",
    ),
    AiConfigField(
        name="ai_timeout_seconds",
        label="Model timeout (s)",
        kind="float",
        default=120.0,
        env_var="AI_TIMEOUT_SECONDS",
        minimum=1.0,
        maximum=3600.0,
        help="Per-generation timeout the ai-service uses when calling the model.",
    ),
    AiConfigField(
        name="ai_temperature",
        label="Temperature",
        kind="float",
        default=0.2,
        env_var="AI_TEMPERATURE",
        minimum=0.0,
        maximum=2.0,
        help="Sampling temperature for the local model.",
    ),
    AiConfigField(
        name="ai_top_p",
        label="Top-p",
        kind="float",
        default=0.9,
        env_var="AI_TOP_P",
        minimum=0.0,
        maximum=1.0,
        help="Nucleus sampling top-p for the local model.",
    ),
    AiConfigField(
        name="ai_max_tokens",
        label="Max tokens",
        kind="int",
        default=1200,
        env_var="AI_MAX_TOKENS",
        minimum=1.0,
        maximum=32768.0,
        help="Maximum tokens the local model may generate per call.",
    ),
    AiConfigField(
        name="ai_keep_alive",
        label="Model keep-alive",
        kind="text",
        default="5m",
        env_var="AI_KEEP_ALIVE",
        help=(
            "How long Ollama keeps the model in VRAM after a call (e.g. 5m, 30s, 0). "
            "A bounded value frees the GPU for WhisperX; '0' unloads immediately, "
            "'-1' pins it forever (not recommended on a single shared GPU)."
        ),
    ),
    AiConfigField(
        name="local_ai_gpu_phase_control_enabled",
        label="GPU phase control",
        kind="choice",
        default="true",
        env_var="LOCAL_AI_GPU_PHASE_CONTROL_ENABLED",
        choices=("true", "false"),
        help=(
            "When on, before WhisperX transcription the system asks Ollama to "
            "release the local model so the two GPU phases do not fight for VRAM. "
            "Only applies when the clip selection backend is ai_service."
        ),
    ),
    AiConfigField(
        name="local_ai_warn_on_gpu_pressure",
        label="Warn on GPU pressure",
        kind="choice",
        default="true",
        env_var="LOCAL_AI_WARN_ON_GPU_PRESSURE",
        choices=("true", "false"),
        help=(
            "When on, the pipeline warns (and recommends a smaller/CPU WhisperX "
            "model) if the local model cannot be released or free VRAM stays low."
        ),
    ),
)

AI_CONFIG_FIELDS_BY_NAME = {field.name: field for field in AI_CONFIG_FIELDS}


def _coerce(field: AiConfigField, raw: str) -> Any:
    text = (raw or "").strip()
    if text == "":
        return field.default
    if field.kind == "choice":
        return text if text in (field.choices or ()) else field.default
    if field.kind == "int":
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return field.default
    if field.kind == "float":
        try:
            return float(text)
        except (TypeError, ValueError):
            return field.default
    return text


def effective_value(field: AiConfigField, saved: dict[str, str]) -> Any:
    """Resolve one field: saved UI value -> env var -> built-in default."""
    if field.name in saved and str(saved.get(field.name) or "").strip() != "":
        return _coerce(field, str(saved.get(field.name)))
    env_raw = os.environ.get(field.env_var, "")
    if env_raw is not None and str(env_raw).strip() != "":
        return _coerce(field, str(env_raw))
    return field.default


def effective_config(saved: dict[str, str]) -> dict[str, Any]:
    """Effective values for every field (saved -> env -> default)."""
    return {field.name: effective_value(field, saved) for field in AI_CONFIG_FIELDS}


def source_for(field_name: str, saved: dict[str, str]) -> str:
    """Where the effective value comes from: 'ui', 'env', or 'default'."""
    field = AI_CONFIG_FIELDS_BY_NAME.get(field_name)
    if field is None:
        return "default"
    if field.name in saved and str(saved.get(field.name) or "").strip() != "":
        return "ui"
    env_raw = os.environ.get(field.env_var, "")
    if env_raw is not None and str(env_raw).strip() != "":
        return "env"
    return "default"


def parse_form(form: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Validate a submitted settings form.

    Returns ``(values, errors)`` where ``values`` maps field name -> string to
    persist. On any error the field is skipped and an error message is added.
    """
    values: dict[str, str] = {}
    errors: list[str] = []
    for field in AI_CONFIG_FIELDS:
        if field.name not in form:
            continue
        raw = str(form.get(field.name) or "").strip()
        if raw == "":
            errors.append(f"{field.label}: value is required.")
            continue
        if field.kind == "choice":
            if raw not in (field.choices or ()):
                errors.append(
                    f"{field.label}: must be one of {', '.join(field.choices or ())}."
                )
                continue
            values[field.name] = raw
            continue
        if field.kind in ("int", "float"):
            try:
                number = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{field.label}: must be a number.")
                continue
            if field.minimum is not None and number < field.minimum:
                errors.append(f"{field.label}: must be >= {field.minimum:g}.")
                continue
            if field.maximum is not None and number > field.maximum:
                errors.append(f"{field.label}: must be <= {field.maximum:g}.")
                continue
            values[field.name] = str(int(number)) if field.kind == "int" else repr(number)
            continue
        values[field.name] = raw
    return values, errors
