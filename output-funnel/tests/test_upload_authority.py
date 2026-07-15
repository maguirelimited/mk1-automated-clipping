"""Truth-table and authority tests for real-upload decision."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from output_funnel.models import UploadStatus
from output_funnel.publisher import upload_due_jobs, upload_one_job
from output_funnel.upload_authority import (
    REASON_CONFIG_DISABLED,
    REASON_CONFIG_UNAVAILABLE,
    REASON_DRY_RUN,
    REASON_NOT_PRODUCTION,
    REASON_RUNTIME_DISABLED,
    REASON_UPLOADS_PAUSED,
    assert_real_upload_permitted,
    config_upload_enabled,
    evaluate_real_upload_decision,
)
from tests.test_social_adapters import (
    ShouldNotRunAdapter,
    SuccessAdapter,
    X_PROFILE,
    _store_with_planned_x_job,
)


def _decision(**kwargs: Any):
    return evaluate_real_upload_decision(**kwargs)


class TestUploadAuthorityTruthTable:
    def test_dev_yaml_enabled_real_rejected(self):
        d = _decision(
            environment="dev",
            upload_mode_value="real",
            yaml_enabled=True,
            runtime_disabled=False,
            paused=False,
        )
        assert d.allow_real_api is False
        assert d.block_reason == REASON_NOT_PRODUCTION

    def test_development_alias_real_rejected(self):
        d = _decision(
            environment="development",
            upload_mode_value="real",
            yaml_enabled=True,
            runtime_disabled=False,
            paused=False,
        )
        assert d.environment == "dev"
        assert d.allow_real_api is False
        assert d.block_reason == REASON_NOT_PRODUCTION

    def test_prod_yaml_disabled_real_blocked(self):
        d = _decision(
            environment="prod",
            upload_mode_value="real",
            yaml_enabled=False,
            runtime_disabled=False,
            paused=False,
        )
        assert d.allow_real_api is False
        assert d.block_reason == REASON_CONFIG_DISABLED

    def test_prod_yaml_enabled_dry_run_no_real_api(self):
        d = _decision(
            environment="prod",
            upload_mode_value="dry_run",
            yaml_enabled=True,
            runtime_disabled=False,
            paused=False,
        )
        assert d.allow_real_api is False
        assert d.block_reason == REASON_DRY_RUN

    def test_prod_yaml_enabled_real_uploads_disabled(self):
        d = _decision(
            environment="prod",
            upload_mode_value="real",
            yaml_enabled=True,
            runtime_disabled=True,
            paused=False,
        )
        assert d.allow_real_api is False
        assert d.block_reason == REASON_RUNTIME_DISABLED

    def test_prod_yaml_enabled_real_uploads_paused(self):
        d = _decision(
            environment="prod",
            upload_mode_value="real",
            yaml_enabled=True,
            runtime_disabled=False,
            paused=True,
        )
        assert d.allow_real_api is False
        assert d.block_reason == REASON_UPLOADS_PAUSED

    def test_prod_yaml_enabled_real_no_pauses_permitted(self):
        d = _decision(
            environment="production",
            upload_mode_value="real",
            yaml_enabled=True,
            runtime_disabled=False,
            paused=False,
        )
        assert d.environment == "prod"
        assert d.allow_real_api is True
        assert d.block_reason is None

    def test_config_upload_enabled_true_cannot_override_yaml_false(self, monkeypatch):
        monkeypatch.setenv("MK04_CONFIG_UPLOAD_ENABLED", "true")
        monkeypatch.setenv("MK04_ENV", "prod")

        def _yaml_false(*, environment=None):
            return False

        monkeypatch.setattr(
            "output_funnel.upload_authority.yaml_uploading_enabled",
            _yaml_false,
        )
        assert config_upload_enabled() is False
        d = _decision(
            environment="prod",
            upload_mode_value="real",
            yaml_enabled=False,
            runtime_disabled=False,
            paused=False,
        )
        assert d.allow_real_api is False
        assert d.block_reason == REASON_CONFIG_DISABLED

    def test_missing_yaml_config_fails_closed(self, monkeypatch):
        monkeypatch.setenv("MK04_ENV", "prod")

        def _boom(*, environment=None):
            raise RuntimeError("uploads_config_unavailable: boom")

        monkeypatch.setattr(
            "output_funnel.upload_authority.load_yaml_uploading_enabled",
            _boom,
        )
        d = evaluate_real_upload_decision(
            environment="prod",
            upload_mode_value="real",
            runtime_disabled=False,
            paused=False,
        )
        assert d.allow_real_api is False
        assert d.block_reason == REASON_CONFIG_UNAVAILABLE
        assert config_upload_enabled() is False

    def test_invalid_upload_mode_fails(self, monkeypatch):
        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.setenv("MK04_UPLOAD_MODE", "weird")
        with pytest.raises(ValueError, match="Invalid MK04_UPLOAD_MODE"):
            from output_funnel.config import upload_mode

            upload_mode()


class TestPublisherFinalGate:
    def test_assert_real_upload_permitted_blocks(self):
        with pytest.raises(RuntimeError, match="real upload denied"):
            assert_real_upload_permitted()

    def test_real_path_checks_before_adapter_call(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
        monkeypatch.setattr(
            "output_funnel.upload_authority.load_yaml_uploading_enabled",
            lambda **_k: True,
        )
        monkeypatch.setattr(
            "output_funnel.upload_authority.runtime_uploads_disabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "output_funnel.upload_authority.uploads_paused",
            lambda: False,
        )

        store, upload_job_id = _store_with_planned_x_job(monkeypatch, tmp_path)
        adapter = SuccessAdapter()
        result = upload_one_job(
            store,
            upload_job_id,
            profiles=[X_PROFILE],
            adapters={"x": adapter},
            max_attempts=3,
        )
        assert result["uploaded"] is True

    def test_final_gate_blocks_adapter_when_paused_mid_flight(
        self, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
        monkeypatch.setattr(
            "output_funnel.upload_authority.load_yaml_uploading_enabled",
            lambda **_k: True,
        )
        monkeypatch.setattr(
            "output_funnel.upload_authority.runtime_uploads_disabled",
            lambda: False,
        )
        # First decision (early upload_block_reason in upload_one_job) allows;
        # final assert sees paused. Simulate by starting unpaused then pausing.
        paused = {"value": False}

        def _paused() -> bool:
            return paused["value"]

        monkeypatch.setattr("output_funnel.upload_authority.uploads_paused", _paused)
        monkeypatch.setattr(
            "output_funnel.runtime_upload_control.upload_block_reason",
            lambda: None,
        )

        store, upload_job_id = _store_with_planned_x_job(monkeypatch, tmp_path)

        class FlipPauseAdapter(ShouldNotRunAdapter):
            def reconcile(self, *args, **kwargs):
                paused["value"] = True
                return None

            def publish(self, *args, **kwargs):
                raise AssertionError("publish must not run when paused")

        result = upload_one_job(
            store,
            upload_job_id,
            profiles=[X_PROFILE],
            adapters={"x": FlipPauseAdapter()},
            max_attempts=3,
        )
        assert result["uploaded"] is False
        assert result["reason"] == REASON_UPLOADS_PAUSED

    def test_dry_run_never_calls_adapter(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.setenv("MK04_UPLOAD_MODE", "dry_run")
        monkeypatch.setattr(
            "output_funnel.upload_authority.load_yaml_uploading_enabled",
            lambda **_k: True,
        )
        store, upload_job_id = _store_with_planned_x_job(monkeypatch, tmp_path)
        result = upload_due_jobs(
            store,
            profiles=[X_PROFILE],
            adapters={"x": ShouldNotRunAdapter()},
            limit=1,
        )
        assert result["uploaded"] == 1
        item = result["results"][0]
        assert item.get("upload_mode") == "dry_run" or store.get_upload_job(upload_job_id)[
            "status"
        ] in {UploadStatus.UPLOADED, "uploaded", UploadStatus.PLANNED}
