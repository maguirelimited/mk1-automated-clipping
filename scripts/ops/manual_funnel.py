#!/usr/bin/env python3
"""Canonical manual-funnel resolution for operator commands and Ops UI.

Precedence:
  1. Explicit funnel id
  2. RUN_FUNNEL_ID
  3. Exactly one active funnel in the environment's source-input catalogue
  4. Fail clearly

Uses source-input funnel_loader (no second registry). Never hard-codes funnel IDs.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManualFunnelError(ValueError):
    """Operator-facing funnel resolution failure."""


@dataclass(frozen=True)
class ManualFunnelResolution:
    funnel_id: str
    source: str  # explicit | run_funnel_id | unique_active
    active: bool
    funnels_file: Path
    warning: str | None = None


def _normalize_env(environment: str) -> str:
    token = (environment or "").strip().lower()
    if token in {"prod", "production"}:
        return "prod"
    if token in {"dev", "development"}:
        return "dev"
    raise ManualFunnelError(
        f"Invalid environment {environment!r}. Expected dev or prod."
    )


def _ensure_input_service_on_path(dev_root: Path | None = None) -> None:
    candidates: list[Path] = []
    if dev_root is not None:
        candidates.append(dev_root / "source-input" / "input_service")
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "source-input" / "input_service")
    for cand in candidates:
        text = str(cand)
        if cand.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
            return


def resolve_source_funnels_path(
    *,
    environment: str,
    dev_root: Path | None = None,
    funnels_file: Path | None = None,
) -> Path:
    """Resolve the source-input funnels.json for the selected environment."""
    if funnels_file is not None:
        path = funnels_file.expanduser()
        if not path.is_file():
            raise ManualFunnelError(f"Funnels file not found: {path}")
        return path.resolve()

    env = _normalize_env(environment)
    explicit = (os.environ.get("SOURCE_INPUT_FUNNELS") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ManualFunnelError(f"SOURCE_INPUT_FUNNELS is not a file: {path}")
        return path.resolve()

    config_dir = (os.environ.get("INPUT_SERVICE_CONFIG_DIR") or "").strip()
    if config_dir:
        path = Path(config_dir).expanduser() / "funnels.json"
        if path.is_file():
            return path.resolve()

    config_root = (os.environ.get("MK04_CONFIG_ROOT") or "").strip()
    if config_root:
        path = Path(config_root).expanduser() / "source-input" / "funnels.json"
        if path.is_file():
            return path.resolve()

    # Environment-specific deploy templates (useful in tests / pre-bootstrap).
    root = (dev_root or Path(__file__).resolve().parents[2]).resolve()
    deploy_candidate = root / "deploy" / "env" / env / "funnels.json"
    if deploy_candidate.is_file():
        return deploy_candidate.resolve()

    if env == "dev":
        repo_candidate = root / "source-input" / "input_service" / "config" / "funnels.json"
        if repo_candidate.is_file():
            return repo_candidate.resolve()

    raise ManualFunnelError(
        f"Cannot locate funnels.json for environment {env}. "
        "Set SOURCE_INPUT_FUNNELS or MK04_CONFIG_ROOT, or supply --funnel-id."
    )


def _list_catalogue(funnels_path: Path, *, dev_root: Path | None) -> list[dict[str, Any]]:
    _ensure_input_service_on_path(dev_root)
    from input_service.funnel_loader import list_funnels  # noqa: PLC0415

    return list_funnels(funnels_file=funnels_path, include_inactive=True)


def resolve_manual_funnel(
    *,
    environment: str,
    explicit_id: str | None = None,
    run_funnel_id: str | None = None,
    funnels_file: Path | None = None,
    dev_root: Path | None = None,
) -> ManualFunnelResolution:
    """Resolve one funnel id for a manual pipeline run."""
    env = _normalize_env(environment)
    funnels_path = resolve_source_funnels_path(
        environment=env, dev_root=dev_root, funnels_file=funnels_file
    )
    catalogue = _list_catalogue(funnels_path, dev_root=dev_root)
    by_id = {
        str(row.get("funnel_id") or "").strip(): row
        for row in catalogue
        if str(row.get("funnel_id") or "").strip()
    }
    active_ids = sorted(
        fid for fid, row in by_id.items() if bool(row.get("active"))
    )

    chosen = (explicit_id or "").strip()
    source = "explicit"
    if not chosen:
        chosen = (run_funnel_id if run_funnel_id is not None else os.environ.get("RUN_FUNNEL_ID") or "").strip()
        source = "run_funnel_id"
    if not chosen:
        if len(active_ids) == 1:
            chosen = active_ids[0]
            source = "unique_active"
        elif len(active_ids) == 0:
            raise ManualFunnelError(
                f"No active funnels configured for {env}. "
                "Supply a funnel id (e.g. `dev <funnel_id>`) or activate exactly one "
                f"funnel in {funnels_path}."
            )
        else:
            listed = ", ".join(active_ids)
            raise ManualFunnelError(
                f"Multiple active funnels for {env}: {listed}. "
                "Supply an explicit funnel id to disambiguate."
            )

    if chosen not in by_id:
        known = ", ".join(sorted(by_id)) or "(none)"
        raise ManualFunnelError(
            f"Unknown funnel_id {chosen!r} for {env}. Known ids: {known}."
        )

    active = bool(by_id[chosen].get("active"))
    warning = None
    if not active:
        # Existing runner accepts the id; source-input may reject inactive at runtime.
        warning = (
            f"Funnel {chosen!r} is marked inactive in {funnels_path.name}; "
            "manual run will still be attempted if the runner accepts it."
        )
    return ManualFunnelResolution(
        funnel_id=chosen,
        source=source,
        active=active,
        funnels_file=funnels_path,
        warning=warning,
    )
