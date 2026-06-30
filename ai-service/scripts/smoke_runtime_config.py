"""Smoke: ai-service load_settings() overlays Ops-UI-saved config.

Verifies the resolution order for model settings:
    Ops UI saved value (controls.json ai_config) -> env var -> built-in default.

This does not call the model or start Flask; it only exercises config loading.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SERVICE_ROOT not in sys.path:
    sys.path.insert(0, SERVICE_ROOT)

import config  # noqa: E402


def _with_controls(ai_config: dict, env: dict) -> config.Settings:
    saved_env = {k: os.environ.get(k) for k in (*env.keys(), "MK04_CONTROLS_FILE")}
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    try:
        json.dump({"ingestion_paused": False, "ai_config": ai_config}, tmp)
        tmp.flush()
        tmp.close()
        os.environ["MK04_CONTROLS_FILE"] = tmp.name
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return config.load_settings()
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        Path(tmp.name).unlink(missing_ok=True)


def main() -> int:
    # UI value wins over env.
    settings = _with_controls(
        {"ai_model": "ui-model:latest", "ai_temperature": "0.4"},
        {"AI_MODEL": "env-model:latest", "AI_TEMPERATURE": "0.9"},
    )
    assert settings.model == "ui-model:latest", settings.model
    assert settings.temperature == 0.4, settings.temperature

    # No UI value -> env wins.
    settings = _with_controls({}, {"AI_MODEL": "env-model:latest"})
    assert settings.model == "env-model:latest", settings.model

    # No UI value, no env -> built-in default.
    settings = _with_controls({}, {"AI_MODEL": None})
    assert settings.model == config.DEFAULT_AI_MODEL, settings.model

    # Invalid UI numeric falls back to env/default.
    settings = _with_controls({"ai_max_tokens": "oops"}, {"AI_MAX_TOKENS": None})
    assert settings.max_tokens == config.DEFAULT_AI_MAX_TOKENS, settings.max_tokens

    print("runtime_config_smoke_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
