"""Secret redaction audit across observability surfaces (Phase 13)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ops"))

from logs_report import redact_line  # noqa: E402
from observability.config_view import redact_config_value  # noqa: E402
from observability.populate import sanitize_detail  # noqa: E402


def test_logs_redact_secrets():
    line = "Authorization: Bearer super-secret-token OPENAI_API_KEY=sk-abcdefghijklmnopqrstuv"
    redacted = redact_line(line)
    assert "super-secret-token" not in redacted
    assert "sk-abcdefghijklmnopqrstuv" not in redacted


def test_config_view_redacts_secret_fields():
    payload = {
        "api_key": "abc",
        "password": "hunter2",
        "client_secret": "xyz",
        "safe": "value",
    }
    redacted = redact_config_value(payload)
    assert redacted["api_key"] == "<redacted>"
    assert redacted["password"] == "<redacted>"
    assert redacted["client_secret"] == "<redacted>"
    assert redacted["safe"] == "value"


def test_health_detail_sanitizer_redacts_assignments():
    detail = sanitize_detail("token=abc123secretvalue path=/var/lib/mk04/prod/db")
    assert detail is not None
    assert "abc123secretvalue" not in detail
    assert "/var/lib" not in detail
