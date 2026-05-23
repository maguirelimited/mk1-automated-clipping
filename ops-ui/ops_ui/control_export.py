from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INGESTION_PAUSED = "ingestion_paused"
UPLOADS_PAUSED = "uploads_paused"
HUMAN_APPROVAL_REQUIRED = "human_approval_required"
PUBLISH_APPROVED_ONLY = "publish_approved_only"


def export_control_flags(
    path: Path,
    *,
    ingestion_paused: bool,
    uploads_paused: bool,
    human_approval_required: bool = False,
    publish_approved_only: bool = False,
) -> None:
    """Mirror UI pause flags to a JSON file services can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        INGESTION_PAUSED: ingestion_paused,
        UPLOADS_PAUSED: uploads_paused,
        HUMAN_APPROVAL_REQUIRED: human_approval_required,
        PUBLISH_APPROVED_ONLY: publish_approved_only,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_controls_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
