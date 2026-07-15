"""Shared fixtures for output-funnel tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_upload_authority_for_adapter_tests(monkeypatch):
    """
    Adapter/publisher regression tests historically exercised real mode under
    MK04_ENV=prod without a deployed controls tree. YAML uploading.enabled is
    now authoritative (prod defaults to false) and Ops UI controls paths are
    prod-root constrained. Provide safe test defaults; individual tests may
    override these patches.

    runtime_uploads_disabled is NOT mocked so control_state.json kill-switch
    tests continue to work.
    """
    monkeypatch.setattr(
        "output_funnel.upload_authority.load_yaml_uploading_enabled",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        "output_funnel.upload_authority.uploads_paused",
        lambda: False,
    )
