"""Tests for output-funnel runtime upload kill switch gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from output_funnel.models import UploadStatus
from output_funnel.publisher import upload_due_jobs
from tests.test_social_adapters import (
    ShouldNotRunAdapter,
    SuccessAdapter,
    X_PROFILE,
    _store_with_planned_x_job,
)


def _enable_yaml_uploads(monkeypatch) -> None:
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


def test_runtime_disabled_blocks_real_upload(monkeypatch, tmp_path: Path):
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    control_file = control_dir / "control_state.json"
    control_file.write_text(json.dumps({"uploads_disabled": True}), encoding="utf-8")

    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    monkeypatch.setenv("MK04_CONTROL_STATE_FILE", str(control_file))
    monkeypatch.setattr(
        "output_funnel.upload_authority.load_yaml_uploading_enabled",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        "output_funnel.upload_authority.uploads_paused",
        lambda: False,
    )
    # Ensure decision reads the real control_state kill switch.
    monkeypatch.setattr(
        "output_funnel.upload_authority.runtime_uploads_disabled",
        lambda: True,
    )

    store, upload_job_id = _store_with_planned_x_job(monkeypatch, tmp_path)
    result = upload_due_jobs(
        store,
        profiles=[X_PROFILE],
        adapters={"x": ShouldNotRunAdapter()},
        limit=1,
    )

    assert result["uploaded"] == 0
    assert result["reason"] == "uploads_disabled_by_runtime_control"
    assert result["results"] == []
    assert store.get_upload_job(upload_job_id)["status"] == UploadStatus.PLANNED


def test_config_disabled_blocks_real_upload(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    monkeypatch.setattr(
        "output_funnel.upload_authority.load_yaml_uploading_enabled",
        lambda **_k: False,
    )
    monkeypatch.setattr(
        "output_funnel.upload_authority.runtime_uploads_disabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "output_funnel.upload_authority.uploads_paused",
        lambda: False,
    )

    store, _upload_job_id = _store_with_planned_x_job(monkeypatch, tmp_path)
    result = upload_due_jobs(
        store,
        profiles=[X_PROFILE],
        adapters={"x": ShouldNotRunAdapter()},
        limit=1,
    )

    assert result["uploaded"] == 0
    assert result["reason"] == "uploads_disabled_by_config"
    assert result["results"] == []


def test_allows_upload_when_config_enabled_and_runtime_not_disabled(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    _enable_yaml_uploads(monkeypatch)

    store, _upload_job_id = _store_with_planned_x_job(monkeypatch, tmp_path)
    result = upload_due_jobs(
        store,
        profiles=[X_PROFILE],
        adapters={"x": SuccessAdapter()},
        limit=1,
    )

    assert result["uploaded"] == 1


def test_production_alias_classified_as_prod_for_upload_gate(monkeypatch, tmp_path: Path):
    """MK04_ENV=production must not be misclassified as non-production."""
    from output_funnel.runtime_upload_control import control_state_path
    from output_funnel.config import runtime_environment, upload_mode

    monkeypatch.setenv("MK04_ENV", "production")
    monkeypatch.delenv("MK04_CONTROL_STATE_FILE", raising=False)
    monkeypatch.delenv("MK04_DATA_ROOT", raising=False)
    monkeypatch.setattr(
        "output_funnel.upload_authority.load_yaml_uploading_enabled",
        lambda **_k: True,
    )

    assert runtime_environment() == "prod"
    assert control_state_path().as_posix().endswith("/data/prod/control_state.json")

    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    assert upload_mode() == "real"


def test_development_alias_not_classified_as_prod(monkeypatch):
    from output_funnel.runtime_upload_control import control_state_path
    from output_funnel.config import runtime_environment, upload_mode

    monkeypatch.setenv("MK04_ENV", "development")
    monkeypatch.delenv("MK04_CONTROL_STATE_FILE", raising=False)
    monkeypatch.delenv("MK04_DATA_ROOT", raising=False)

    assert runtime_environment() == "dev"
    assert control_state_path().as_posix().endswith("/data/dev/control_state.json")

    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    with pytest.raises(RuntimeError, match="only allowed"):
        upload_mode()


def test_unset_mk04_env_never_enables_upload_heuristic(monkeypatch):
    from output_funnel.upload_authority import config_upload_enabled
    from output_funnel.config import runtime_environment

    monkeypatch.delenv("MK04_ENV", raising=False)
    monkeypatch.setattr(
        "output_funnel.upload_authority.yaml_uploading_enabled",
        lambda **_k: False,
    )

    assert runtime_environment() == "dev"
    assert config_upload_enabled() is False
