"""Tests for the simplified Create Funnel flow (Funnel Management MK1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.funnel_management.create import parse_funnel_create_form
from ops_ui.funnel_management.create_defaults import (
    BASELINE_AI_RULE_PROFILE,
    BASELINE_CREATE_ENABLED,
    BASELINE_CREATE_STATUS,
    BASELINE_ENVIRONMENT,
    BASELINE_TEMPLATE_ID,
    DEFAULT_CREATE_CONFIG_MANAGER_PRESET,
)
from ops_ui.funnel_management.schema import dump_canonical_funnel, load_canonical_funnel


TEMPLATE_ID = BASELINE_TEMPLATE_ID
FUNNEL_ID = "football_boots_001"
SOURCE_URL = "https://www.youtube.com/@example/videos"


def _settings(
    tmp_path: Path,
    *,
    auth_enabled: bool = False,
    password: str = "secret-pass",
) -> Settings:
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
        auth_enabled=auth_enabled,
        operator_password=password,
        secret_key="test-secret-key",
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
        ),
    )


def _login(client, password: str = "secret-pass") -> None:
    page = client.get("/login")
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    token = html.split(marker, 1)[1].split('"', 1)[0]
    client.post(
        "/login",
        data={"password": password, "csrf_token": token, "next": "/funnels/new"},
    )


def _csrf_token(client) -> str:
    page = client.get("/funnels/new")
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    return html.split(marker, 1)[1].split('"', 1)[0]


def _valid_form(**overrides: str) -> dict[str, str]:
    data = {
        "template_id": TEMPLATE_ID,
        "funnel_id": FUNNEL_ID,
        "display_name": "Football Boots",
        "description": "Draft funnel for tests",
        "category": "sports",
        "source_type": "youtube_channel",
        "source_urls": SOURCE_URL,
    }
    data.update(overrides)
    return data


@pytest.fixture
def registry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))
    return registry_dir


class TestGetForm:
    def test_get_renders_minimal_fields(self, tmp_path: Path, registry_env: Path) -> None:
        response = create_app(_settings(tmp_path)).test_client().get("/funnels/new")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "Create Funnel" in body
        assert 'name="funnel_id"' in body
        assert 'name="display_name"' in body
        assert 'name="category"' in body
        assert 'name="source_urls"' in body
        assert 'name="source_type"' in body
        assert 'name="template_id"' in body
        assert 'name="ai_profile_mode"' not in body
        assert 'name="config_manager_preset_id"' not in body

    def test_baseline_template_is_default(self, tmp_path: Path, registry_env: Path) -> None:
        body = create_app(_settings(tmp_path)).test_client().get("/funnels/new").data.decode("utf-8")
        assert "Baseline Stream Clips" in body
        assert f'value="{BASELINE_TEMPLATE_ID}"' in body


class TestBaselineDefaults:
    def test_parse_minimal_form(self) -> None:
        parsed, errors = parse_funnel_create_form(_valid_form())
        assert errors == []
        assert parsed is not None
        assert parsed.template_id == BASELINE_TEMPLATE_ID
        assert parsed.source_urls == (SOURCE_URL,)

    def test_post_applies_baseline_defaults(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form()
        data["csrf_token"] = _csrf_token(client)
        client.post("/funnels/new", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["identity"]["status"] == BASELINE_CREATE_STATUS
        assert saved["identity"]["enabled"] is BASELINE_CREATE_ENABLED
        assert saved["identity"]["environment"] == BASELINE_ENVIRONMENT
        assert saved["processing"]["ai_rules"]["ai_rule_profile"] == BASELINE_AI_RULE_PROFILE
        assert saved["processing"]["ai_rules"]["prompt_managed"] == "builtin"
        assert saved["mappings"]["config_manager_preset_id"] == DEFAULT_CREATE_CONFIG_MANAGER_PRESET
        assert saved["distribution"]["posting_enabled"] is False
        assert len(saved["acquisition"]["sources"]) == 1


class TestSuccessfulCreate:
    def test_post_creates_registry_file_and_redirects(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form()
        data["csrf_token"] = _csrf_token(client)
        response = client.post("/funnels/new", data=data, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/funnels/{FUNNEL_ID}")

        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["identity"]["funnel_id"] == FUNNEL_ID
        assert saved["identity"]["display_name"] == "Football Boots"
        assert saved["identity"]["template_source"] == TEMPLATE_ID
        assert "readiness" not in saved

    def test_created_funnel_appears_on_list(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form()
        data["csrf_token"] = _csrf_token(client)
        client.post("/funnels/new", data=data)
        list_body = client.get("/funnels").data
        assert FUNNEL_ID.encode() in list_body
        assert b"Football Boots" in list_body


class TestSourceHandling:
    def test_multiple_source_urls(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form(
            source_urls="https://www.youtube.com/@one/videos\nhttps://www.youtube.com/@two/videos",
        )
        data["csrf_token"] = _csrf_token(client)
        client.post("/funnels/new", data=data)
        funnel = load_canonical_funnel(json.loads((registry_env / f"{FUNNEL_ID}.json").read_text()))
        assert len(funnel.acquisition.sources) == 2

    def test_playlist_source_type(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form(
            funnel_id="youtube_playlist_test_001",
            display_name="Playlist Test Funnel",
            source_type="youtube_playlist",
            source_urls="https://www.youtube.com/playlist?list=PLtestfixture",
        )
        data["csrf_token"] = _csrf_token(client)
        client.post("/funnels/new", data=data)
        funnel = load_canonical_funnel(
            json.loads((registry_env / "youtube_playlist_test_001.json").read_text(encoding="utf-8"))
        )
        assert funnel.acquisition.source_type == "youtube_playlist"
        assert funnel.acquisition.sources[0].source_type == "youtube_playlist"

    def test_no_source_input_config_written(self, tmp_path: Path, registry_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        source_path = tmp_path / "source_funnels.json"
        source_path.write_text("[]", encoding="utf-8")
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(source_path))

        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form()
        data["csrf_token"] = _csrf_token(client)
        client.post("/funnels/new", data=data)
        assert source_path.read_text(encoding="utf-8") == "[]"


class TestInvalidHandling:
    def test_missing_source_url(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form(source_urls="")
        data["csrf_token"] = _csrf_token(client)
        response = client.post("/funnels/new", data=data)
        assert response.status_code == 200
        assert b"source URL" in response.data

    def test_duplicate_funnel_id(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form()
        data["csrf_token"] = _csrf_token(client)
        client.post("/funnels/new", data=data)
        response = client.post("/funnels/new", data=data)
        assert response.status_code == 200
        assert b"already exists" in response.data
        assert len(list(registry_env.glob("*.json"))) == 1

    def test_invalid_funnel_id(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form(funnel_id="Bad-ID")
        data["csrf_token"] = _csrf_token(client)
        response = client.post("/funnels/new", data=data)
        assert response.status_code == 200
        assert b"funnel_id" in response.data.lower() or b"lowercase" in response.data.lower()
        assert not (registry_env / "Bad-ID.json").exists()

    def test_unknown_template_id(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form(template_id="missing_template")
        data["csrf_token"] = _csrf_token(client)
        response = client.post("/funnels/new", data=data)
        assert response.status_code == 200
        assert b"Unknown funnel template" in response.data

    def test_invalid_source_url(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form(source_urls="not-a-url")
        data["csrf_token"] = _csrf_token(client)
        response = client.post("/funnels/new", data=data)
        assert response.status_code == 200
        assert b"url" in response.data.lower()


class TestCsrfAndSecurity:
    def test_csrf_required_when_auth_enabled(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path, auth_enabled=True)).test_client()
        _login(client)
        response = client.post("/funnels/new", data=_valid_form())
        assert response.status_code == 200
        assert b"Invalid security token" in response.data
        assert list(registry_env.glob("*.json")) == []

    def test_get_does_not_create_files(self, tmp_path: Path, registry_env: Path) -> None:
        create_app(_settings(tmp_path)).test_client().get("/funnels/new")
        assert list(registry_env.glob("*.json")) == []

    def test_path_traversal_funnel_id_blocked(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form(funnel_id="../evil")
        data["csrf_token"] = _csrf_token(client)
        response = client.post("/funnels/new", data=data)
        assert response.status_code == 200
        assert list(registry_env.glob("*.json")) == []
        assert not (registry_env.parent / "evil.json").exists()


class TestScopeProtection:
    def test_no_edit_clone_sync_buttons_on_form(self, tmp_path: Path, registry_env: Path) -> None:
        body = create_app(_settings(tmp_path)).test_client().get("/funnels/new").data.decode("utf-8").lower()
        for forbidden in ("clone funnel", "sync funnel", "edit funnel", "delete funnel"):
            assert forbidden not in body

    def test_create_form_exposes_playlist_source_type(self, tmp_path: Path, registry_env: Path) -> None:
        body = create_app(_settings(tmp_path)).test_client().get("/funnels/new").data.decode("utf-8")
        assert 'name="source_type"' in body
        assert "YouTube playlist" in body
        assert "YouTube channel" in body

    def test_saved_funnel_has_no_forbidden_fields(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _valid_form()
        data["csrf_token"] = _csrf_token(client)
        client.post("/funnels/new", data=data)
        dumped = dump_canonical_funnel(
            load_canonical_funnel(json.loads((registry_env / f"{FUNNEL_ID}.json").read_text()))
        )
        forbidden = {"readiness", "operations", "pause_state", "prompt_text", "oauth", "credentials"}
        assert forbidden.isdisjoint(dumped.keys())
