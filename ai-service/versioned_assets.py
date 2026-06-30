from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"
SCHEMAS_DIR = BASE_DIR / "schemas"
VERSION_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class VersionedAssetError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def load_prompt(prompt_version: str) -> str:
    _validate_version(prompt_version, asset_name="prompt")
    path = _safe_asset_path(PROMPTS_DIR, f"{prompt_version}.txt")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VersionedAssetError(
            "PROMPT_VERSION_NOT_FOUND",
            f"Prompt version not found: {prompt_version}",
            status_code=404,
        ) from exc


def load_schema(schema_version: str) -> dict[str, Any]:
    _validate_version(schema_version, asset_name="schema")
    path = _safe_asset_path(SCHEMAS_DIR, f"{schema_version}.json")
    try:
        with path.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)
    except FileNotFoundError as exc:
        raise VersionedAssetError(
            "SCHEMA_VERSION_NOT_FOUND",
            f"Schema version not found: {schema_version}",
            status_code=404,
        ) from exc
    except json.JSONDecodeError as exc:
        raise VersionedAssetError(
            "SCHEMA_INVALID_JSON",
            f"Schema version is not valid JSON: {schema_version}",
            status_code=500,
        ) from exc

    if not isinstance(schema, dict):
        raise VersionedAssetError(
            "SCHEMA_INVALID_JSON",
            f"Schema version must contain a JSON object: {schema_version}",
            status_code=500,
        )
    return schema


def list_prompt_versions() -> list[str]:
    return _list_versions(PROMPTS_DIR, ".txt")


def list_schema_versions() -> list[str]:
    return _list_versions(SCHEMAS_DIR, ".json")


def _validate_version(version: str, *, asset_name: str) -> None:
    if not VERSION_RE.fullmatch(version):
        if asset_name == "prompt":
            raise VersionedAssetError(
                "INVALID_PROMPT_VERSION",
                "Prompt version must contain only letters, numbers, underscores, and hyphens.",
                status_code=400,
            )
        raise VersionedAssetError(
            "INVALID_SCHEMA_VERSION",
            "Schema version must contain only letters, numbers, underscores, and hyphens.",
            status_code=400,
        )


def _safe_asset_path(root: Path, filename: str) -> Path:
    root_resolved = root.resolve()
    path = (root_resolved / filename).resolve()
    if path.parent != root_resolved:
        raise VersionedAssetError(
            "INVALID_ASSET_PATH",
            "Versioned assets must be loaded from the configured asset folder.",
            status_code=400,
        )
    return path


def _list_versions(root: Path, suffix: str) -> list[str]:
    if not root.is_dir():
        return []
    versions = []
    for path in root.iterdir():
        if path.is_file() and path.name.endswith(suffix):
            versions.append(path.name[: -len(suffix)])
    return sorted(versions)
