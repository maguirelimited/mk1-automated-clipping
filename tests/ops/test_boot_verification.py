"""Tests for boot verification (Reliability Phase 5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(OPS_DIR))

import boot_verification as boot  # noqa: E402


def _probe_all_ok(url: str) -> tuple[bool, str]:
    return True, "HTTP 200"


def _probe_core_only(url: str) -> tuple[bool, str]:
    # Required services use /healthz; optional AI / ops-ui use /health.
    if "/healthz" in url:
        return True, "HTTP 200"
    return False, "connection refused"


def _probe_api_down(url: str) -> tuple[bool, str]:
    if any(p in url for p in (":5060/", ":5160/")):
        return False, "connection refused"
    return True, "HTTP 200"


class TestBootVerificationReady:
    def test_ready_when_required_pass_even_if_optional_down(self):
        report = boot.build_boot_verification("dev", probe_fn=_probe_core_only)
        assert report.overall == "READY"
        by_label = {c.label: c for c in report.components}
        assert by_label["API"].result == "PASS"
        assert by_label["Worker"].result == "PASS"
        assert by_label["Output funnel"].result == "PASS"
        assert by_label["AI service"].result == "WARN"
        assert by_label["AI service"].required is False
        assert by_label["Operations UI"].required is False

    def test_not_ready_when_api_down(self):
        report = boot.build_boot_verification("dev", probe_fn=_probe_api_down)
        assert report.overall == "NOT READY"
        api = next(c for c in report.components if c.label == "API")
        assert api.result == "FAIL"
        assert api.required is True

    def test_render_includes_boot_readiness(self):
        report = boot.build_boot_verification("dev", probe_fn=_probe_all_ok)
        text = boot.render_boot_verification(report)
        assert "Boot Verification:" in text
        assert "Boot readiness" in text
        assert report.overall in text
        assert "(required)" in text
        assert "(optional)" in text

    def test_to_dict_lists_failures(self):
        report = boot.build_boot_verification("dev", probe_fn=_probe_api_down)
        payload = report.to_dict()
        assert payload["overall"] == "NOT READY"
        assert "API" in payload["required_failures"]


class TestBootVerificationCli:
    def test_help(self):
        assert boot.main(["--help"]) == 0

    def test_not_ready_exit_code(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            boot,
            "build_boot_verification",
            lambda _env, **_kwargs: boot.BootVerification(
                environment="dev",
                env_label="DEVELOPMENT",
                components=[
                    boot.BootComponent("API", "FAIL", "down", True),
                ],
                overall="NOT READY",
            ),
        )
        assert boot.main(["dev"]) == 2
