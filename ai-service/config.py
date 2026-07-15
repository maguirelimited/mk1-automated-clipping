from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from shared.controls_file import read_controls_json, resolve_controls_path  # noqa: E402

DEFAULT_AI_PROVIDER = "ollama"
DEFAULT_AI_MODEL = "qwen2.5:14b-instruct"
DEFAULT_AI_BASE_URL = "http://localhost:11434"
DEFAULT_AI_TIMEOUT_SECONDS = 120.0
DEFAULT_AI_TEMPERATURE = 0.2
DEFAULT_AI_TOP_P = 0.9
DEFAULT_AI_MAX_TOKENS = 1200
# Ollama keep-alive for the loaded model. "5m" matches Ollama's own default and
# means the 14B model is NOT pinned in VRAM forever: it is evicted after the
# idle window, so the GPU is available for the next WhisperX transcription. A
# value of "0" unloads immediately after each call; "-1" would pin it forever
# (not recommended on a single shared GPU).
DEFAULT_AI_KEEP_ALIVE = "5m"
DEFAULT_AI_SERVICE_HOST = "127.0.0.1"
DEFAULT_AI_SERVICE_PORT = 5075


@dataclass(frozen=True)
class Settings:
    provider: str = DEFAULT_AI_PROVIDER
    model: str = DEFAULT_AI_MODEL
    base_url: str = DEFAULT_AI_BASE_URL
    timeout_seconds: float = DEFAULT_AI_TIMEOUT_SECONDS
    temperature: float = DEFAULT_AI_TEMPERATURE
    top_p: float = DEFAULT_AI_TOP_P
    max_tokens: int = DEFAULT_AI_MAX_TOKENS
    keep_alive: str = DEFAULT_AI_KEEP_ALIVE
    service_host: str = DEFAULT_AI_SERVICE_HOST
    service_port: int = DEFAULT_AI_SERVICE_PORT
    service_debug: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "keep_alive": self.keep_alive,
            "service_host": self.service_host,
            "service_port": self.service_port,
            "service_debug": self.service_debug,
        }


def load_settings() -> Settings:
    # The Ops UI is the control plane: it writes saved model config into the
    # shared controls.json (ai_config block). Resolution per field is
    # UI saved value -> environment variable -> built-in default. The service
    # bind host/port are deployment concerns and stay env-only.
    ui = _load_ui_ai_config()
    return Settings(
        provider=_resolve_str("AI_PROVIDER", ui.get("ai_provider"), DEFAULT_AI_PROVIDER).lower(),
        model=_resolve_str("AI_MODEL", ui.get("ai_model"), DEFAULT_AI_MODEL),
        base_url=_resolve_str("AI_BASE_URL", ui.get("ai_base_url"), DEFAULT_AI_BASE_URL).rstrip("/"),
        timeout_seconds=_resolve_float(
            "AI_TIMEOUT_SECONDS", ui.get("ai_timeout_seconds"), DEFAULT_AI_TIMEOUT_SECONDS
        ),
        temperature=_resolve_float("AI_TEMPERATURE", ui.get("ai_temperature"), DEFAULT_AI_TEMPERATURE),
        top_p=_resolve_float("AI_TOP_P", ui.get("ai_top_p"), DEFAULT_AI_TOP_P),
        max_tokens=_resolve_int("AI_MAX_TOKENS", ui.get("ai_max_tokens"), DEFAULT_AI_MAX_TOKENS),
        keep_alive=_resolve_str("AI_KEEP_ALIVE", ui.get("ai_keep_alive"), DEFAULT_AI_KEEP_ALIVE),
        service_host=_env_str("AI_SERVICE_HOST", DEFAULT_AI_SERVICE_HOST),
        service_port=_env_int("AI_SERVICE_PORT", DEFAULT_AI_SERVICE_PORT),
        service_debug=_env_bool("AI_SERVICE_DEBUG", False),
    )


def _controls_file_path() -> Path:
    return resolve_controls_path()


def _load_ui_ai_config() -> dict[str, Any]:
    """Read the Ops-UI-saved ai_config block. Returns {} on any problem."""
    data = read_controls_json()
    block = data.get("ai_config")
    return block if isinstance(block, dict) else {}


def _resolve_str(env_name: str, ui_value: Any, default: str) -> str:
    if ui_value is not None and str(ui_value).strip() != "":
        return str(ui_value).strip()
    return _env_str(env_name, default)


def _resolve_float(env_name: str, ui_value: Any, default: float) -> float:
    if ui_value is not None and str(ui_value).strip() != "":
        try:
            return float(str(ui_value).strip())
        except ValueError:
            pass
    return _env_float(env_name, default)


def _resolve_int(env_name: str, ui_value: Any, default: int) -> int:
    if ui_value is not None and str(ui_value).strip() != "":
        try:
            return int(float(str(ui_value).strip()))
        except ValueError:
            pass
    return _env_int(env_name, default)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}
