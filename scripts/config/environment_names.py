"""
Canonical environment-name normalization for mk04.

Runtime token (preferred everywhere after entrypoint boundaries):
    dev | prod

ConfigManager public form (retained for resolved config / snapshots):
    development | production

Accepted aliases (all map through this module — do not compare raw strings):
    dev, development → runtime "dev", config "development"
    prod, production → runtime "prod", config "production"

Invalid values are rejected. Unset handling is call-site specific:
    ConfigManager may default to development; nothing may default to production.
"""

from __future__ import annotations

from typing import Final

RUNTIME_DEV: Final = "dev"
RUNTIME_PROD: Final = "prod"
CONFIG_DEV: Final = "development"
CONFIG_PROD: Final = "production"

_RUNTIME_ALIASES: Final[dict[str, str]] = {
    "dev": RUNTIME_DEV,
    "development": RUNTIME_DEV,
    "prod": RUNTIME_PROD,
    "production": RUNTIME_PROD,
}

_CONFIG_FROM_RUNTIME: Final[dict[str, str]] = {
    RUNTIME_DEV: CONFIG_DEV,
    RUNTIME_PROD: CONFIG_PROD,
}

_RUNTIME_FROM_CONFIG: Final[dict[str, str]] = {
    CONFIG_DEV: RUNTIME_DEV,
    CONFIG_PROD: RUNTIME_PROD,
}

ACCEPTED_ALIASES: Final[tuple[str, ...]] = tuple(sorted(_RUNTIME_ALIASES.keys()))


class EnvironmentNameError(ValueError):
    """Raised when an environment name cannot be normalized."""


def _format_invalid(raw: str | None) -> str:
    shown = "<missing>" if raw is None or str(raw).strip() == "" else repr(raw)
    return (
        f"Invalid environment: {shown}. "
        f"Expected one of: {', '.join(ACCEPTED_ALIASES)}."
    )


def normalize_runtime_env(raw: str) -> str:
    """
    Normalize any accepted alias to the canonical runtime token: 'dev' | 'prod'.

    Raises EnvironmentNameError for missing/blank/unknown values.
    """
    if raw is None or not str(raw).strip():
        raise EnvironmentNameError(_format_invalid(raw))
    token = _RUNTIME_ALIASES.get(str(raw).strip().lower())
    if token is None:
        raise EnvironmentNameError(_format_invalid(raw))
    return token


def to_config_environment(runtime_or_alias: str) -> str:
    """
    Normalize any accepted alias (or runtime/config token) to ConfigManager form:
    'development' | 'production'.
    """
    runtime = normalize_runtime_env(runtime_or_alias)
    return _CONFIG_FROM_RUNTIME[runtime]


def to_runtime_env(config_or_alias: str) -> str:
    """
    Normalize any accepted alias (or runtime/config token) to runtime form:
    'dev' | 'prod'.

    Equivalent to normalize_runtime_env; kept for call-site clarity at
    ConfigManager → deploy boundaries.
    """
    return normalize_runtime_env(config_or_alias)


def is_production_env(raw: str | None, *, default: str = RUNTIME_DEV) -> bool:
    """
    Return True when the value (or default when raw is unset/blank) is production.

    ``default`` must itself be an accepted alias. Unknown non-blank values raise.
    Never treats an unset value as production when default is development/dev.
    """
    if raw is None or not str(raw).strip():
        return normalize_runtime_env(default) == RUNTIME_PROD
    return normalize_runtime_env(raw) == RUNTIME_PROD


def is_development_env(raw: str | None, *, default: str = RUNTIME_DEV) -> bool:
    return not is_production_env(raw, default=default)


def resolve_mk04_env(
    *,
    explicit: str | None = None,
    environ_value: str | None = None,
    default: str | None = RUNTIME_DEV,
) -> str:
    """
    Resolve runtime env with priority: explicit → environ_value → default.

    When ``default`` is None and both explicit and environ_value are unset,
    raises EnvironmentNameError (callers that must require an explicit env).
    """
    if explicit is not None and str(explicit).strip() != "":
        return normalize_runtime_env(explicit)
    if environ_value is not None and str(environ_value).strip() != "":
        return normalize_runtime_env(environ_value)
    if default is None:
        raise EnvironmentNameError(_format_invalid(None))
    return normalize_runtime_env(default)


def env_label(runtime_or_alias: str) -> str:
    """Return DEVELOPMENT or PRODUCTION for banners / status."""
    return "PRODUCTION" if is_production_env(runtime_or_alias) else "DEVELOPMENT"
