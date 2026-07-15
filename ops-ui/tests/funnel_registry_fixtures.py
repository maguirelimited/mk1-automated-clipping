"""Shared funnel rule registry fixtures for ops-ui tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def registry_document(*, include_funnel_alias: bool = True, extra_aliases: dict[str, str] | None = None) -> dict[str, Any]:
    aliases = {
        "business": "business",
        "business_ai": "business",
        "finance": "finance",
    }
    if include_funnel_alias:
        aliases["mfm_business_ai_001"] = "business"
    if extra_aliases:
        aliases.update(extra_aliases)
    return {
        "schema_version": 1,
        "profiles": {
            "business": {"rules_version": "business_v1", "managed": "builtin"},
            "finance": {"rules_version": "finance_v1", "managed": "builtin"},
            "sport": {"rules_version": "sport_v1", "managed": "builtin"},
            "comedy": {"rules_version": "comedy_v1", "managed": "builtin"},
        },
        "aliases": aliases,
    }


def write_registry(path: Path, document: dict[str, Any] | None = None) -> Path:
    payload = document if document is not None else registry_document()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
