from __future__ import annotations

import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ai_settings  # noqa: E402


def _write_controls(tmp_path, ai_config):
    path = tmp_path / "controls.json"
    path.write_text(
        json.dumps({"ingestion_paused": False, "ai_config": ai_config}),
        encoding="utf-8",
    )
    return path


def test_backend_default_is_ai_service(monkeypatch, tmp_path):
    monkeypatch.delenv("CLIP_SELECTION_BACKEND", raising=False)
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    assert ai_settings.resolve_clip_selection_backend() == "ai_service"


def test_openai_backend_available_for_rollback(monkeypatch, tmp_path):
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("CLIP_SELECTION_BACKEND", "openai")
    assert ai_settings.resolve_clip_selection_backend() == "openai"


def test_env_var_used_when_no_ui_value(monkeypatch, tmp_path):
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("CLIP_SELECTION_BACKEND", "ai_service")
    assert ai_settings.resolve_clip_selection_backend() == "ai_service"


def test_ui_value_overrides_env(monkeypatch, tmp_path):
    path = _write_controls(tmp_path, {"clip_selection_backend": "openai"})
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(path))
    monkeypatch.setenv("CLIP_SELECTION_BACKEND", "ai_service")
    # UI says openai, env says ai_service -> UI wins.
    assert ai_settings.resolve_clip_selection_backend() == "openai"


def test_per_run_option_overrides_everything(monkeypatch, tmp_path):
    path = _write_controls(tmp_path, {"clip_selection_backend": "openai"})
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(path))
    monkeypatch.setenv("CLIP_SELECTION_BACKEND", "openai")
    assert ai_settings.resolve_clip_selection_backend("ai_service") == "ai_service"


def test_backend_aliases_normalize_to_ai_service(monkeypatch, tmp_path):
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.delenv("CLIP_SELECTION_BACKEND", raising=False)
    for alias in ("ai-service", "local", "ollama", "AI_SERVICE"):
        assert ai_settings.resolve_clip_selection_backend(alias) == "ai_service"


def test_invalid_controls_file_falls_back_to_env(monkeypatch, tmp_path):
    bad = tmp_path / "controls.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(bad))
    monkeypatch.setenv("CLIP_SELECTION_BACKEND", "ai_service")
    assert ai_settings.resolve_clip_selection_backend() == "ai_service"


def test_service_url_priority(monkeypatch, tmp_path):
    path = _write_controls(tmp_path, {"ai_service_url": "http://ui-host:9999/"})
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(path))
    monkeypatch.setenv("AI_SERVICE_URL", "http://env-host:1234")
    assert ai_settings.resolve_ai_service_url() == "http://ui-host:9999"

    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    assert ai_settings.resolve_ai_service_url() == "http://env-host:1234"

    monkeypatch.delenv("AI_SERVICE_URL", raising=False)
    assert ai_settings.resolve_ai_service_url() == ai_settings.DEFAULT_AI_SERVICE_URL


def test_service_timeout_priority_and_validation(monkeypatch, tmp_path):
    path = _write_controls(tmp_path, {"ai_service_timeout_seconds": "240.0"})
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(path))
    assert ai_settings.resolve_ai_service_timeout_seconds() == 240.0

    # Invalid UI value -> fall through to env.
    path = _write_controls(tmp_path, {"ai_service_timeout_seconds": "nonsense"})
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(path))
    monkeypatch.setenv("AI_SERVICE_TIMEOUT_SECONDS", "90")
    assert ai_settings.resolve_ai_service_timeout_seconds() == 90.0

    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.delenv("AI_SERVICE_TIMEOUT_SECONDS", raising=False)
    assert (
        ai_settings.resolve_ai_service_timeout_seconds()
        == ai_settings.DEFAULT_AI_SERVICE_TIMEOUT_SECONDS
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
