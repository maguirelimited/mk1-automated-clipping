"""Tests for canonical environment_names normalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG = REPO_ROOT / "scripts" / "config"
if str(SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG))

from environment_names import (  # noqa: E402
    EnvironmentNameError,
    env_label,
    is_development_env,
    is_production_env,
    normalize_runtime_env,
    resolve_mk04_env,
    to_config_environment,
    to_runtime_env,
)


class TestNormalizeRuntimeEnv:
    def test_dev_aliases(self):
        assert normalize_runtime_env("dev") == "dev"
        assert normalize_runtime_env("development") == "dev"
        assert normalize_runtime_env("DEV") == "dev"
        assert normalize_runtime_env("Development") == "dev"

    def test_prod_aliases(self):
        assert normalize_runtime_env("prod") == "prod"
        assert normalize_runtime_env("production") == "prod"
        assert normalize_runtime_env("PROD") == "prod"
        assert normalize_runtime_env("Production") == "prod"

    def test_invalid_rejected(self):
        with pytest.raises(EnvironmentNameError, match="Invalid environment"):
            normalize_runtime_env("staging")
        with pytest.raises(EnvironmentNameError):
            normalize_runtime_env("")
        with pytest.raises(EnvironmentNameError):
            normalize_runtime_env("   ")


class TestConfigForm:
    def test_aliases_to_config_form(self):
        assert to_config_environment("dev") == "development"
        assert to_config_environment("development") == "development"
        assert to_config_environment("prod") == "production"
        assert to_config_environment("production") == "production"

    def test_round_trip(self):
        for alias in ("dev", "development", "prod", "production"):
            runtime = normalize_runtime_env(alias)
            config = to_config_environment(alias)
            assert to_runtime_env(config) == runtime


class TestProductionClassification:
    def test_production_aliases_are_production(self):
        assert is_production_env("prod") is True
        assert is_production_env("production") is True
        assert is_development_env("prod") is False

    def test_development_aliases_are_not_production(self):
        assert is_production_env("dev") is False
        assert is_production_env("development") is False
        assert is_development_env("development") is True

    def test_unset_never_production(self):
        assert is_production_env(None) is False
        assert is_production_env("") is False
        assert is_production_env(None, default="dev") is False
        assert is_production_env(None, default="development") is False

    def test_unset_with_prod_default_is_explicit(self):
        assert is_production_env(None, default="prod") is True


class TestResolveMk04Env:
    def test_explicit_wins(self):
        assert resolve_mk04_env(explicit="production", environ_value="dev") == "prod"

    def test_environ_when_no_explicit(self):
        assert resolve_mk04_env(explicit=None, environ_value="development") == "dev"

    def test_default_development(self):
        assert resolve_mk04_env(explicit=None, environ_value=None) == "dev"

    def test_require_explicit_when_default_none(self):
        with pytest.raises(EnvironmentNameError):
            resolve_mk04_env(explicit=None, environ_value=None, default=None)


class TestLabels:
    def test_labels(self):
        assert env_label("dev") == "DEVELOPMENT"
        assert env_label("production") == "PRODUCTION"
