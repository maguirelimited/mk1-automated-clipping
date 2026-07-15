"""Read Ops-UI-saved local-AI settings from the shared controls file.

The Ops UI is the control plane: it writes ``controls.json`` (under an
``ai_config`` block). video-automation reads that same shared file directly so
it does NOT need to call ops-ui over HTTP just to learn the configured
clip-selection backend or ai-service endpoint.

Resolution order (first definite value wins):

    1. per-run selection option (passed in explicitly by the caller)
    2. Ops UI saved value (controls.json -> ai_config)
    3. environment variable
    4. safe built-in default

If the shared file is missing or invalid, this falls back cleanly to the
environment variable and then the default, so existing env-only setups keep
working unchanged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from shared.controls_file import read_controls_json, resolve_controls_path  # noqa: E402

DEFAULT_CLIP_SELECTION_BACKEND = "ai_service"
DEFAULT_AI_SERVICE_URL = "http://127.0.0.1:5075"
DEFAULT_AI_SERVICE_TIMEOUT_SECONDS = 180.0
DEFAULT_AI_BASE_URL = "http://localhost:11434"
DEFAULT_AI_MODEL = "qwen2.5:14b-instruct"
DEFAULT_GPU_PHASE_CONTROL_ENABLED = True
DEFAULT_WARN_ON_GPU_PRESSURE = True

# Accepted aliases that all mean "use the local ai-service backend".
_AI_SERVICE_ALIASES = {"ai_service", "ai-service", "local", "ollama"}

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def controls_file_path() -> Path:
    return resolve_controls_path()


def read_ai_config() -> dict[str, Any]:
    """Return the saved ``ai_config`` block, or ``{}`` on any problem."""
    data = read_controls_json()
    block = data.get("ai_config")
    return block if isinstance(block, dict) else {}


def _ui_value(name: str) -> str:
    value = read_ai_config().get(name)
    return str(value).strip() if value is not None else ""


def _normalize_backend(raw: str) -> str:
    backend = (raw or "").strip().lower()
    if not backend:
        return ""
    return "ai_service" if backend in _AI_SERVICE_ALIASES else "openai"


def resolve_clip_selection_backend(per_run: str | None = None) -> str:
    """Resolve the clip-selection backend with the documented priority."""
    per_run_backend = _normalize_backend(per_run or "")
    if per_run_backend:
        return per_run_backend
    ui_backend = _normalize_backend(_ui_value("clip_selection_backend"))
    if ui_backend:
        return ui_backend
    env_backend = _normalize_backend(os.environ.get("CLIP_SELECTION_BACKEND", ""))
    if env_backend:
        return env_backend
    return DEFAULT_CLIP_SELECTION_BACKEND


def resolve_ai_service_url() -> str:
    ui_url = _ui_value("ai_service_url")
    if ui_url:
        return ui_url.rstrip("/")
    env_url = os.environ.get("AI_SERVICE_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return DEFAULT_AI_SERVICE_URL


def resolve_ai_service_timeout_seconds() -> float:
    for source in (_ui_value("ai_service_timeout_seconds"), os.environ.get("AI_SERVICE_TIMEOUT_SECONDS", "").strip()):
        if source:
            try:
                value = float(source)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return DEFAULT_AI_SERVICE_TIMEOUT_SECONDS


def resolve_ai_base_url() -> str:
    """Resolve the Ollama backend base URL: UI saved value -> env -> default.

    This mirrors the precedence ``ai-service`` itself uses, so the GPU phase
    controller talks to the same Ollama instance the model judging will use.
    """
    ui_url = _ui_value("ai_base_url")
    if ui_url:
        return ui_url.rstrip("/")
    env_url = os.environ.get("AI_BASE_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return DEFAULT_AI_BASE_URL


def resolve_ai_model() -> str:
    """Resolve the configured local model tag: UI saved value -> env -> default."""
    ui_model = _ui_value("ai_model")
    if ui_model:
        return ui_model
    env_model = os.environ.get("AI_MODEL", "").strip()
    if env_model:
        return env_model
    return DEFAULT_AI_MODEL


def _resolve_bool(ui_key: str, env_name: str, default: bool) -> bool:
    for raw in (_ui_value(ui_key), os.environ.get(env_name, "").strip()):
        token = (raw or "").strip().lower()
        if token in _TRUTHY:
            return True
        if token in _FALSY:
            return False
    return default


def resolve_gpu_phase_control_enabled() -> bool:
    """Whether local-AI GPU phase control runs before transcription.

    UI saved value -> ``LOCAL_AI_GPU_PHASE_CONTROL_ENABLED`` env -> default True.
    """
    return _resolve_bool(
        "local_ai_gpu_phase_control_enabled",
        "LOCAL_AI_GPU_PHASE_CONTROL_ENABLED",
        DEFAULT_GPU_PHASE_CONTROL_ENABLED,
    )


def resolve_warn_on_gpu_pressure() -> bool:
    """Whether to emit a VRAM-pressure warning when the local model cannot be
    released. UI saved value -> ``LOCAL_AI_WARN_ON_GPU_PRESSURE`` env -> True.
    """
    return _resolve_bool(
        "local_ai_warn_on_gpu_pressure",
        "LOCAL_AI_WARN_ON_GPU_PRESSURE",
        DEFAULT_WARN_ON_GPU_PRESSURE,
    )
