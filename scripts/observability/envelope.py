"""Versioned API envelope for observability JSON endpoints.

Wraps contract payloads without changing their fields or meaning.
Future endpoints should use the same shape so warnings, pagination, and
caching metadata have a stable home.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Envelope format version (API transport). Distinct from CONTRACT_SCHEMA_VERSION
# on models inside ``data``.
API_ENVELOPE_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def observability_envelope(
    data: dict[str, Any] | None,
    *,
    generated_at: str | None = None,
    schema_version: int = API_ENVELOPE_SCHEMA_VERSION,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a contract payload in the standard observability API envelope.

    ``data`` may be null when ``error`` describes a missing resource.
    Optional future fields (warnings, pagination, caching) can be added
    alongside these keys without changing contract models.
    """
    payload: dict[str, Any] = {
        "schema_version": int(schema_version),
        "generated_at": generated_at or _utc_now_iso(),
        "data": data,
    }
    if error is not None:
        payload["error"] = error
    return payload


def not_found_error(
    *,
    resource: str,
    resource_id: str,
    message: str | None = None,
) -> dict[str, Any]:
    """Structured error body for missing runs/jobs."""
    return {
        "code": "not_found",
        "resource": resource,
        "id": resource_id,
        "message": message or f"{resource} not found",
    }
