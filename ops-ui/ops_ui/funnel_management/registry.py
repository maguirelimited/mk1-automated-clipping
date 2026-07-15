"""Local JSON registry for canonical funnel definitions."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .schema import (
    CanonicalFunnel,
    CanonicalFunnelSchemaError,
    dump_canonical_funnel,
    load_canonical_funnel,
)

_FUNNEL_ID_RE = re.compile(r"^[a-z0-9_]+$")
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _PACKAGE_ROOT / "ops-ui" / "data"
_REGISTRY_ENV_VAR = "OPS_FUNNEL_REGISTRY_DIR"
_DATA_DIR_ENV_VAR = "OPS_UI_DATA_DIR"


class FunnelRegistryError(Exception):
    """Base class for funnel registry errors."""


class FunnelNotFoundError(FunnelRegistryError):
    """Raised when a funnel file does not exist in the registry."""


class DuplicateFunnelError(FunnelRegistryError):
    """Raised when saving would overwrite an existing funnel without permission."""


class FunnelRegistryPathError(FunnelRegistryError):
    """Raised when a funnel ID or path is unsafe or inconsistent."""


def default_registry_dir() -> Path:
    """Resolve the default canonical funnel registry directory."""
    override = os.environ.get(_REGISTRY_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    data_raw = os.environ.get(_DATA_DIR_ENV_VAR, "").strip()
    data_dir = Path(data_raw).expanduser() if data_raw else _DEFAULT_DATA_DIR
    return (data_dir / "funnel_registry").resolve()


class FunnelRegistry:
    """Load and save canonical funnels as one JSON file per funnel_id."""

    def __init__(self, registry_dir: Path | str | None = None) -> None:
        self.registry_dir = (
            default_registry_dir()
            if registry_dir is None
            else Path(registry_dir).expanduser().resolve()
        )

    def list_funnels(self) -> list[CanonicalFunnel]:
        """Load all ``*.json`` funnels in the registry, sorted by funnel_id."""
        if not self.registry_dir.is_dir():
            return []

        funnels: list[CanonicalFunnel] = []
        for path in sorted(self.registry_dir.glob("*.json")):
            if not path.is_file():
                continue
            funnels.append(self.load_file(path))
        return sorted(funnels, key=lambda funnel: funnel.identity.funnel_id)

    def get_funnel(self, funnel_id: str) -> CanonicalFunnel:
        """Load one funnel by ID."""
        path = self._funnel_path(funnel_id)
        if not path.is_file():
            raise FunnelNotFoundError(f"Funnel not found: {funnel_id!r}")
        return self.load_file(path)

    def exists(self, funnel_id: str) -> bool:
        """Return whether ``<funnel_id>.json`` exists in the registry."""
        path = self._funnel_path(funnel_id)
        return path.is_file()

    def save_funnel(self, funnel: CanonicalFunnel, *, overwrite: bool = False) -> Path:
        """Write one canonical funnel to ``<funnel_id>.json``."""
        funnel_id = funnel.identity.funnel_id
        path = self._funnel_path(funnel_id)
        if path.exists() and not overwrite:
            raise DuplicateFunnelError(
                f"Funnel already exists: {funnel_id!r} (set overwrite=True to replace)"
            )

        self.registry_dir.mkdir(parents=True, exist_ok=True)
        payload = dump_canonical_funnel(funnel)
        self._write_json_atomic(path, payload)
        return path

    def load_file(self, path: Path | str) -> CanonicalFunnel:
        """Load and validate one registry JSON file."""
        resolved = self._resolve_registry_file(path)
        try:
            raw_text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise FunnelRegistryError(f"Could not read registry file {resolved}: {exc}") from exc

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise FunnelRegistryError(
                f"Invalid JSON in registry file {resolved.name}: {exc.msg}"
            ) from exc

        if not isinstance(data, dict):
            raise FunnelRegistryError(f"Registry file {resolved.name} must contain a JSON object")

        try:
            funnel = load_canonical_funnel(data)
        except CanonicalFunnelSchemaError as exc:
            raise FunnelRegistryError(
                f"Invalid canonical funnel in {resolved.name}: {exc}"
            ) from exc

        expected_id = resolved.stem
        if funnel.identity.funnel_id != expected_id:
            raise FunnelRegistryPathError(
                f"Registry filename {resolved.name!r} does not match "
                f"identity.funnel_id {funnel.identity.funnel_id!r}"
            )
        return funnel

    def _funnel_path(self, funnel_id: str) -> Path:
        safe_id = self._validate_funnel_id(funnel_id)
        path = (self.registry_dir / f"{safe_id}.json").resolve()
        if path.parent != self.registry_dir.resolve():
            raise FunnelRegistryPathError(
                f"Unsafe funnel path for {funnel_id!r}: path escapes registry directory"
            )
        return path

    def _resolve_registry_file(self, path: Path | str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = (self.registry_dir / candidate).resolve()
        else:
            candidate = candidate.resolve()

        registry_root = self.registry_dir.resolve()
        if candidate != registry_root and registry_root not in candidate.parents:
            raise FunnelRegistryPathError(
                f"Registry file {candidate} is outside registry directory {registry_root}"
            )
        if candidate.suffix.lower() != ".json":
            raise FunnelRegistryPathError(f"Registry file must be a .json file: {candidate.name}")
        return candidate

    @staticmethod
    def _validate_funnel_id(funnel_id: str) -> str:
        if not isinstance(funnel_id, str) or not funnel_id.strip():
            raise FunnelRegistryPathError("funnel_id must be a non-empty string")
        clean = funnel_id.strip()
        if len(clean) > 128:
            raise FunnelRegistryPathError("funnel_id is too long")
        if not _FUNNEL_ID_RE.match(clean):
            raise FunnelRegistryPathError(
                "funnel_id must contain only lowercase letters, numbers, and underscores"
            )
        if clean != funnel_id:
            raise FunnelRegistryPathError("funnel_id must not contain leading or trailing whitespace")
        return clean

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        if not encoded.endswith("\n"):
            encoded += "\n"

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
