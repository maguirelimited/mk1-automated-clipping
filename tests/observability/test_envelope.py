"""Tests for the observability API envelope."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from observability.envelope import (  # noqa: E402
    API_ENVELOPE_SCHEMA_VERSION,
    observability_envelope,
)


def test_envelope_wraps_data_without_mutating_payload():
    data = {"overall": "PASS", "environment": "dev", "schema_version": 1}
    envelope = observability_envelope(data, generated_at="2026-07-04T12:34:56Z")
    assert envelope == {
        "schema_version": API_ENVELOPE_SCHEMA_VERSION,
        "generated_at": "2026-07-04T12:34:56Z",
        "data": data,
    }
    assert envelope["data"] is data


def test_envelope_generates_timestamp_when_omitted():
    envelope = observability_envelope({"ok": True})
    assert envelope["schema_version"] == 1
    assert envelope["generated_at"].endswith("Z")
    assert envelope["data"] == {"ok": True}


def test_envelope_supports_not_found_error():
    from observability.envelope import not_found_error

    envelope = observability_envelope(
        None,
        generated_at="2026-07-04T12:34:56Z",
        error=not_found_error(resource="run", resource_id="run_missing"),
    )
    assert envelope["data"] is None
    assert envelope["error"]["code"] == "not_found"
    assert envelope["error"]["resource"] == "run"
    assert envelope["error"]["id"] == "run_missing"
