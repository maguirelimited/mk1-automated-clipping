"""Authoritative real-upload decision for output-funnel.

Formula (every condition restrictive — none grant permission alone):

  real platform API call allowed iff ALL of:
    1. normalized environment is prod
    2. ConfigManager/YAML uploading.enabled is true
    3. MK04_UPLOAD_MODE is real
    4. runtime uploads_disabled is false
    5. Ops UI uploads_paused is false

MK04_CONFIG_UPLOAD_ENABLED is a transport mirror of YAML only. It must never
override uploading.enabled=false. Missing/malformed config fails closed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control_gate import uploads_paused
from .env_names import is_production_env, resolve_mk04_env


REASON_NOT_PRODUCTION = "not_production"
REASON_CONFIG_DISABLED = "uploads_disabled_by_config"
REASON_CONFIG_UNAVAILABLE = "uploads_config_unavailable"
REASON_DRY_RUN = "upload_mode_dry_run"
REASON_INVALID_MODE = "invalid_upload_mode"
REASON_RUNTIME_DISABLED = "uploads_disabled_by_runtime_control"
REASON_UPLOADS_PAUSED = "uploads_paused_by_ops_control"
REASON_CONTROLS_UNAVAILABLE = "uploads_controls_unavailable"


@dataclass(frozen=True)
class UploadDecision:
    """Result of evaluating whether a real platform API call may proceed."""

    allow_real_api: bool
    environment: str
    yaml_upload_enabled: bool | None
    upload_mode: str
    runtime_uploads_disabled: bool
    uploads_paused: bool
    block_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_real_api": self.allow_real_api,
            "environment": self.environment,
            "yaml_upload_enabled": self.yaml_upload_enabled,
            "upload_mode": self.upload_mode,
            "runtime_uploads_disabled": self.runtime_uploads_disabled,
            "uploads_paused": self.uploads_paused,
            "block_reason": self.block_reason,
        }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ensure_config_scripts_on_path() -> None:
    scripts_config = _repo_root() / "scripts" / "config"
    text = str(scripts_config)
    if text not in sys.path:
        sys.path.insert(0, text)


def load_yaml_uploading_enabled(*, environment: str | None = None) -> bool:
    """
    Load uploading.enabled from ConfigManager.

    Raises RuntimeError on missing/malformed config (fail closed).
    """
    _ensure_config_scripts_on_path()
    from config_manager import ConfigError, ConfigManager  # noqa: PLC0415
    from environment_names import EnvironmentNameError, to_config_environment  # noqa: PLC0415

    env_raw = environment
    if env_raw is None or not str(env_raw).strip():
        env_raw = resolve_mk04_env(environ_value=os.environ.get("MK04_ENV"), default="dev")
    try:
        canonical = to_config_environment(env_raw)
    except EnvironmentNameError as exc:
        raise RuntimeError(f"uploads_config_unavailable: invalid environment {env_raw!r}") from exc

    config_root = _repo_root() / "config"
    try:
        resolved = ConfigManager.load(environment=canonical, config_root=config_root)
    except ConfigError as exc:
        raise RuntimeError(f"uploads_config_unavailable: {exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"uploads_config_unavailable: {exc.__class__.__name__}") from exc
    return bool(resolved.uploading_enabled)


def yaml_uploading_enabled(*, environment: str | None = None) -> bool | None:
    """Return YAML uploading.enabled, or None when config cannot be loaded."""
    try:
        return load_yaml_uploading_enabled(environment=environment)
    except RuntimeError:
        return None


def config_upload_enabled() -> bool:
    """
    Configuration upload authority = YAML uploading.enabled.

    MK04_CONFIG_UPLOAD_ENABLED, when set, is treated only as a derived mirror:
    it cannot enable uploads when YAML is false. If YAML cannot be loaded,
    fail closed (False) regardless of the transport variable.
    """
    yaml_enabled = yaml_uploading_enabled()
    if yaml_enabled is None:
        return False
    if not yaml_enabled:
        return False
    # YAML says true. Transport var may only confirm or be absent — never elevate.
    raw = os.environ.get("MK04_CONFIG_UPLOAD_ENABLED", "").strip().lower()
    if raw in {"false", "0", "no"}:
        # Stale/mismatched transport claiming false while YAML true: trust YAML
        # as authority (env.sh should keep them in sync).
        return True
    return True


def runtime_uploads_disabled() -> bool:
    from .runtime_upload_control import runtime_uploads_disabled as _runtime_disabled

    return _runtime_disabled()


def read_upload_mode() -> str:
    from .config import upload_mode

    return upload_mode()


def _safe_uploads_paused() -> tuple[bool, str | None]:
    """Return (paused, error_reason). Fail closed when controls cannot be read."""
    try:
        return bool(uploads_paused()), None
    except RuntimeError:
        return True, REASON_CONTROLS_UNAVAILABLE


def evaluate_real_upload_decision(
    *,
    environment: str | None = None,
    upload_mode_value: str | None = None,
    yaml_enabled: bool | None = None,
    runtime_disabled: bool | None = None,
    paused: bool | None = None,
) -> UploadDecision:
    """
    Evaluate whether a real platform API call is permitted.

    Optional overrides are for tests; production call sites leave them unset.
    """
    env = resolve_mk04_env(
        explicit=environment,
        environ_value=os.environ.get("MK04_ENV") if environment is None else None,
        default="dev",
    )

    if yaml_enabled is None:
        try:
            yaml_val: bool | None = load_yaml_uploading_enabled(environment=env)
            yaml_error: str | None = None
        except RuntimeError:
            yaml_val = None
            yaml_error = REASON_CONFIG_UNAVAILABLE
    else:
        yaml_val = yaml_enabled
        yaml_error = None

    if upload_mode_value is None:
        try:
            mode = read_upload_mode()
        except (RuntimeError, ValueError) as exc:
            controls_err = None
            if paused is None:
                _, controls_err = _safe_uploads_paused()
            return UploadDecision(
                allow_real_api=False,
                environment=env,
                yaml_upload_enabled=yaml_val,
                upload_mode="invalid",
                runtime_uploads_disabled=bool(runtime_disabled)
                if runtime_disabled is not None
                else runtime_uploads_disabled(),
                uploads_paused=True if controls_err else (bool(paused) if paused is not None else False),
                block_reason=REASON_INVALID_MODE if isinstance(exc, ValueError) else str(exc),
            )
    else:
        mode = str(upload_mode_value).strip().lower()

    disabled = (
        bool(runtime_disabled)
        if runtime_disabled is not None
        else runtime_uploads_disabled()
    )
    controls_error: str | None = None
    if paused is not None:
        paused_flag = bool(paused)
    else:
        paused_flag, controls_error = _safe_uploads_paused()

    block: str | None = None
    if env != "prod":
        block = REASON_NOT_PRODUCTION
    elif yaml_error is not None:
        block = yaml_error
    elif yaml_val is not True:
        block = REASON_CONFIG_DISABLED
    elif mode == "dry_run":
        block = REASON_DRY_RUN
    elif mode != "real":
        block = REASON_INVALID_MODE
    elif disabled:
        block = REASON_RUNTIME_DISABLED
    elif controls_error is not None:
        block = controls_error
    elif paused_flag:
        block = REASON_UPLOADS_PAUSED

    return UploadDecision(
        allow_real_api=block is None,
        environment=env,
        yaml_upload_enabled=yaml_val,
        upload_mode=mode,
        runtime_uploads_disabled=disabled,
        uploads_paused=paused_flag,
        block_reason=block,
    )


def upload_block_reason() -> str | None:
    """Reason real uploads are blocked, or None when real API calls are allowed."""
    decision = evaluate_real_upload_decision()
    if decision.allow_real_api:
        return None
    # dry_run is not an "upload block" for batch skip semantics when mode is dry_run —
    # callers that only run in real mode still see it; publisher branches on mode first.
    return decision.block_reason


def assert_real_upload_permitted() -> UploadDecision:
    """
    Final gate immediately before a real platform API call.

    Raises RuntimeError when real posting is not permitted.
    """
    decision = evaluate_real_upload_decision()
    if not decision.allow_real_api:
        reason = decision.block_reason or "uploads_blocked"
        raise RuntimeError(f"real upload denied: {reason}")
    return decision
