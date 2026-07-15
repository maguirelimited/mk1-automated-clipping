from __future__ import annotations

import json
from pathlib import Path

from ops_ui.ai_config import effective_config, parse_form, source_for
from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.store import ControlStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        # Unreachable on purpose so health probes fail fast without hanging.
        ai_service_url="http://127.0.0.1:9",
        ai_diagnostics_timeout_sec=0.01,
        services=(
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )


def test_store_ai_config_roundtrip_and_export(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "ops.sqlite3", controls_file=tmp_path / "controls.json")
    store.init_db()
    store.set_ai_config({"clip_selection_backend": "ai_service", "ai_model": "qwen2.5:14b-instruct"})

    assert store.get_ai_config()["clip_selection_backend"] == "ai_service"

    exported = json.loads((tmp_path / "controls.json").read_text(encoding="utf-8"))
    assert exported["ai_config"]["clip_selection_backend"] == "ai_service"
    # Boolean control flags must still be present alongside the AI block.
    assert "ingestion_paused" in exported


def test_effective_config_env_fallback(monkeypatch) -> None:
    monkeypatch.delenv("CLIP_SELECTION_BACKEND", raising=False)
    monkeypatch.setenv("AI_MODEL", "env-model:latest")
    saved = {"clip_selection_backend": "ai_service"}
    effective = effective_config(saved)
    assert effective["clip_selection_backend"] == "ai_service"  # from saved UI
    assert effective["ai_model"] == "env-model:latest"  # from env
    assert effective["ai_provider"] == "ollama"  # built-in default
    assert source_for("clip_selection_backend", saved) == "ui"
    assert source_for("ai_model", saved) == "env"
    assert source_for("ai_provider", saved) == "default"


def test_parse_form_validates_choice_and_numbers() -> None:
    values, errors = parse_form(
        {
            "clip_selection_backend": "ai_service",
            "ai_temperature": "0.4",
            "ai_max_tokens": "800",
        }
    )
    assert not errors
    assert values["clip_selection_backend"] == "ai_service"
    assert values["ai_max_tokens"] == "800"

    _, errors = parse_form({"clip_selection_backend": "totally_invalid"})
    assert errors
    _, errors = parse_form({"ai_temperature": "not-a-number"})
    assert errors


def test_settings_page_renders_with_ai_section_when_offline(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    response = app.test_client().get("/settings")
    assert response.status_code == 200
    assert b"Local AI" in response.data
    assert b"Clip selection backend" in response.data
    # Unreachable ai-service must render a degraded status, not crash.
    assert b"ai-service reachable" in response.data


def test_post_ai_settings_persists(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    client = app.test_client()
    resp = client.post(
        "/settings/ai",
        data={"clip_selection_backend": "ai_service", "ai_model": "qwen2.5:14b-instruct"},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)
    exported = json.loads((tmp_path / "controls.json").read_text(encoding="utf-8"))
    assert exported["ai_config"]["clip_selection_backend"] == "ai_service"


def test_post_ai_settings_rejects_invalid(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = app.test_client()
    resp = client.post(
        "/settings/ai",
        data={"clip_selection_backend": "nonsense"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    controls_path = tmp_path / "controls.json"
    if controls_path.is_file():
        exported = json.loads(controls_path.read_text(encoding="utf-8"))
        assert "clip_selection_backend" not in exported.get("ai_config", {})


def test_model_test_button_handles_unreachable(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    resp = app.test_client().post("/settings/ai/test", follow_redirects=False)
    # Renders the settings page with a degraded diagnostic; never 500s.
    assert resp.status_code == 200
    assert b"diagnostics/model status" in resp.data


def _fake_ai_health(url: str, timeout: float) -> dict:
    return {
        "reachable": False,
        "status": "unreachable",
        "status_code": None,
        "provider": None,
        "model_configured": None,
        "backend_reachable": False,
        "model_available": False,
        "error": "mock",
        "checked_at": "now",
    }


def test_health_probe_uses_effective_saved_ai_service_url(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
    store.init_db()
    store.set_ai_config({"ai_service_url": "http://127.0.0.1:5999"})

    probed: list[str] = []

    def _capture_health(url: str, timeout: float) -> dict:
        probed.append(url)
        return _fake_ai_health(url, timeout)

    monkeypatch.setattr("ops_ui.app.ai_health", _capture_health)

    app = create_app(settings)
    response = app.test_client().get("/settings")
    assert response.status_code == 200
    assert probed == ["http://127.0.0.1:5999"]
    assert b"http://127.0.0.1:5999" in response.data
    assert settings.ai_service_url.encode() not in response.data


def test_health_probe_falls_back_to_settings_url_without_ui_or_env(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.delenv("AI_SERVICE_URL", raising=False)
    monkeypatch.delenv("OPS_AI_SERVICE_URL", raising=False)

    probed: list[str] = []

    def _capture_health(url: str, timeout: float) -> dict:
        probed.append(url)
        return _fake_ai_health(url, timeout)

    monkeypatch.setattr("ops_ui.app.ai_health", _capture_health)

    app = create_app(settings)
    app.test_client().get("/settings")
    assert probed == [settings.ai_service_url]


def test_health_probe_uses_env_ai_service_url_when_no_ui_saved(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("AI_SERVICE_URL", "http://127.0.0.1:5888")

    probed: list[str] = []

    def _capture_health(url: str, timeout: float) -> dict:
        probed.append(url)
        return _fake_ai_health(url, timeout)

    monkeypatch.setattr("ops_ui.app.ai_health", _capture_health)

    app = create_app(settings)
    app.test_client().get("/settings")
    assert probed == ["http://127.0.0.1:5888"]
