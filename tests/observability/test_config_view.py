"""Tests for configuration view (Phase 12)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from observability.config_view import build_config_view, redact_config_value  # noqa: E402


class TestRedaction:
    def test_redacts_secret_keys_and_values(self):
        payload = {
            "api_key": "should-not-appear",
            "model": "placeholder-local-model",
            "nested": {"password": "hunter2", "ok": True},
            "token_value": "sk-abcdefghijklmnopqrstuvwxyz0123456789",
        }
        redacted = redact_config_value(payload)
        assert redacted["api_key"] == "<redacted>"
        assert redacted["nested"]["password"] == "<redacted>"
        assert redacted["model"] == "placeholder-local-model"
        assert "hunter2" not in str(redacted)
        assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in str(redacted)


class TestConfigView:
    def test_builds_view_from_config_manager(self):
        view = build_config_view("dev")
        assert view["environment"] == "dev"
        assert view["environment_label"] == "DEVELOPMENT"
        assert view["validation"]["state"] in {"PASS", "FAIL"}
        assert view["summary"]["funnel_id"] == "business"
        assert view["summary"]["platform_id"] == "youtube"
        assert view["summary"]["preset_id"]
        assert "upload" in view
        assert "scheduler" in view
        assert "retention" in view
        assert "ai" in view
        assert view["resolved_config_available"] is True
        assert "api_key" not in str(view["resolved_config"]).lower() or "<redacted>" in str(
            view["resolved_config"]
        )
        # Paths are environment-relative labels, not absolute secrets.
        assert view["paths"]["jobs_root"] == "jobs/dev"
        assert not str(view["paths"]["jobs_root"]).startswith("/")

    def test_prod_environment_label(self):
        view = build_config_view("prod")
        assert view["environment"] == "prod"
        assert view["environment_label"] == "PRODUCTION"
