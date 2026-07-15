"""Tests for conditional production secret validation."""

from __future__ import annotations

import json
from pathlib import Path

from production_secrets import (
    validate_production_secrets,
)


def _channels(tmp_path: Path, *, enabled: bool = True) -> Path:
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "channel_id": "yt_main",
                        "platform": "youtube_shorts",
                        "enabled": enabled,
                        "credentials": {
                            "token_file_env": "TEST_YT_TOKEN_FILE",
                            "client_secret_file_env": "TEST_YT_CLIENT_SECRET_FILE",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


class TestProductionSecrets:
    def test_local_ai_does_not_require_openai(self, tmp_path: Path):
        result = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=False,
            upload_mode="dry_run",
            channels_path=_channels(tmp_path),
            environ={
                "CLIP_SELECTION_BACKEND": "ai_service",
                "AI_PROVIDER": "ollama",
            },
        )
        assert result.ok is True
        assert "OPENAI_API_KEY" not in result.required_names
        assert any("OPENAI_API_KEY not required" in w for w in result.warnings)

    def test_openai_backend_requires_key(self, tmp_path: Path):
        result = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=False,
            upload_mode="dry_run",
            channels_path=_channels(tmp_path),
            environ={"CLIP_SELECTION_BACKEND": "openai"},
        )
        assert result.ok is False
        assert "OPENAI_API_KEY" in result.required_names
        assert any("OPENAI_API_KEY" in e for e in result.errors)
        joined = " ".join(result.errors)
        assert "sk-" not in joined

    def test_openai_placeholder_rejected(self, tmp_path: Path):
        result = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=False,
            upload_mode="dry_run",
            channels_path=_channels(tmp_path),
            environ={
                "CLIP_SELECTION_BACKEND": "openai",
                "OPENAI_API_KEY": "changeme",
            },
        )
        assert result.ok is False
        assert any("placeholder" in e for e in result.errors)

    def test_uploading_disabled_boots_without_platform_creds(self, tmp_path: Path):
        result = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=False,
            upload_mode="dry_run",
            channels_path=_channels(tmp_path),
            environ={"CLIP_SELECTION_BACKEND": "ai_service"},
        )
        assert result.ok is True
        assert "TEST_YT_TOKEN_FILE" not in result.required_names

    def test_real_upload_requires_destination_credentials(self, tmp_path: Path):
        token = tmp_path / "token.json"
        secret = tmp_path / "client.json"
        # Missing files → fail
        result = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=True,
            upload_mode="real",
            channels_path=_channels(tmp_path),
            environ={
                "CLIP_SELECTION_BACKEND": "ai_service",
                "TEST_YT_TOKEN_FILE": str(token),
                "TEST_YT_CLIENT_SECRET_FILE": str(secret),
            },
        )
        assert result.ok is False
        assert any("TEST_YT_TOKEN_FILE" in e for e in result.errors)

        token.write_text('{"token":"x"}', encoding="utf-8")
        secret.write_text('{"installed":{}}', encoding="utf-8")
        ok = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=True,
            upload_mode="real",
            channels_path=_channels(tmp_path),
            environ={
                "CLIP_SELECTION_BACKEND": "ai_service",
                "TEST_YT_TOKEN_FILE": str(token),
                "TEST_YT_CLIENT_SECRET_FILE": str(secret),
            },
        )
        assert ok.ok is True

    def test_disabled_channel_does_not_require_secrets(self, tmp_path: Path):
        result = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=True,
            upload_mode="real",
            channels_path=_channels(tmp_path, enabled=False),
            environ={"CLIP_SELECTION_BACKEND": "ai_service"},
        )
        # No enabled profiles → error about missing profiles, not specific secrets
        assert result.ok is False
        assert any("no enabled channel" in e for e in result.errors)

    def test_require_flag_false_skips_all(self, tmp_path: Path):
        result = validate_production_secrets(
            require_production_secrets=False,
            uploading_enabled=True,
            upload_mode="real",
            channels_path=_channels(tmp_path),
            environ={"CLIP_SELECTION_BACKEND": "openai"},
        )
        assert result.ok is True
        assert result.errors == []

    def test_errors_never_include_secret_values(self, tmp_path: Path):
        secret_value = "sk-live-super-secret-value-do-not-leak"
        result = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=False,
            upload_mode="dry_run",
            channels_path=_channels(tmp_path),
            environ={
                "CLIP_SELECTION_BACKEND": "openai",
                "OPENAI_API_KEY": "",
            },
        )
        blob = json.dumps(result.to_dict())
        assert secret_value not in blob
        assert "sk-live" not in blob
