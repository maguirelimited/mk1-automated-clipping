from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from config import Settings, load_settings


def _coerce_keep_alive(raw: Any) -> Any:
    """Normalise a configured keep_alive value for the Ollama API.

    Ollama accepts either a number of seconds or a duration string such as
    ``"5m"``. Numeric-looking values are sent as numbers (so ``"0"`` reliably
    unloads); duration strings are passed through. An empty value means "do not
    send", letting Ollama apply its own default.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        if text.lstrip("-").isdigit():
            return int(text)
        return float(text)
    except ValueError:
        return text


class ModelClientError(RuntimeError):
    """Controlled model-backend error suitable for API responses."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ModelResponse:
    text: str | None
    model_used: str
    provider: str
    raw_response: dict[str, Any] | None
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model_used": self.model_used,
            "provider": self.provider,
            "raw_response": self.raw_response,
            "error": self.error,
        }


class OllamaModelClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        if self.settings.provider != "ollama":
            raise ModelClientError(
                "UNSUPPORTED_PROVIDER",
                f"Unsupported AI_PROVIDER={self.settings.provider!r}; only 'ollama' is enabled in MK1.",
            )

    @property
    def provider(self) -> str:
        return self.settings.provider

    @property
    def model(self) -> str:
        return self.settings.model

    def backend_reachable(self) -> bool:
        return self.backend_status().get("backend_reachable") is True

    def model_available(self) -> bool:
        return self.backend_status().get("model_available") is True

    def backend_status(self) -> dict[str, Any]:
        try:
            payload = self._get_tags()
        except requests.RequestException as exc:
            return {
                "backend_reachable": False,
                "model_available": False,
                "error": str(exc),
            }
        except ValueError as exc:
            return {
                "backend_reachable": True,
                "model_available": False,
                "error": f"invalid Ollama tags response: {exc}",
            }

        models = payload.get("models") if isinstance(payload, dict) else None
        model_available = False
        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                names = {str(item.get("name") or ""), str(item.get("model") or "")}
                if self.settings.model in names:
                    model_available = True
                    break
        return {
            "backend_reachable": True,
            "model_available": model_available,
            "error": None,
        }

    def generate(self, prompt: str) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.settings.temperature,
                "top_p": self.settings.top_p,
                "num_predict": self.settings.max_tokens,
            },
        }
        # keep_alive controls how long Ollama keeps the (large) model resident in
        # VRAM after this call. A bounded value frees the GPU for the next
        # WhisperX transcription instead of pinning the 14B model forever.
        keep_alive = _coerce_keep_alive(self.settings.keep_alive)
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        try:
            response = requests.post(
                f"{self.settings.base_url}/api/generate",
                json=payload,
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            raw_response = response.json()
        except requests.RequestException as exc:
            return self._error_response(str(exc))
        except ValueError as exc:
            return self._error_response(f"Ollama returned invalid JSON: {exc}")

        if not isinstance(raw_response, dict):
            return self._error_response("Ollama returned an unexpected response shape.")
        if raw_response.get("error"):
            return self._error_response(str(raw_response["error"]), raw_response=raw_response)

        text = raw_response.get("response")
        if not isinstance(text, str):
            return self._error_response("Ollama response did not include text output.", raw_response=raw_response)

        return ModelResponse(
            text=text,
            model_used=self.settings.model,
            provider=self.settings.provider,
            raw_response=raw_response,
            error=None,
        )

    def _get_tags(self) -> dict[str, Any]:
        response = requests.get(
            f"{self.settings.base_url}/api/tags",
            timeout=min(max(self.settings.timeout_seconds, 0.1), 5.0),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("expected JSON object")
        return payload

    def _error_response(
        self,
        error: str,
        *,
        raw_response: dict[str, Any] | None = None,
    ) -> ModelResponse:
        return ModelResponse(
            text=None,
            model_used=self.settings.model,
            provider=self.settings.provider,
            raw_response=raw_response,
            error=error,
        )
