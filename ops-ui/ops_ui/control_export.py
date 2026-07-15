from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from shared.controls_file import read_controls_json_at  # noqa: E402

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
    ai_config: dict[str, str] | None = None,
    processing_config: dict[str, str] | None = None,
    post_processing_config: dict[str, str] | None = None,
) -> None:
    """Mirror UI pause flags (and saved AI config) to a JSON file services read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        INGESTION_PAUSED: ingestion_paused,
        UPLOADS_PAUSED: uploads_paused,
        HUMAN_APPROVAL_REQUIRED: human_approval_required,
        PUBLISH_APPROVED_ONLY: publish_approved_only,
        # Only operator-saved overrides are written. Unset fields are omitted
        # so each service can still fall back to its env var / built-in default.
        "ai_config": dict(ai_config or {}),
        "processing_config": dict(processing_config or {}),
        "post_processing_config": dict(post_processing_config or {}),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_controls_file(path: Path) -> dict[str, Any]:
    return read_controls_json_at(path)
