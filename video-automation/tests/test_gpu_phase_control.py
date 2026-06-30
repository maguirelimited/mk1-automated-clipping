from __future__ import annotations

import os
import sys

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import gpu_phase_control as gpc  # noqa: E402


# --- Fake transports / runners -------------------------------------------------


class FakeHttp:
    """Records calls; configurable per-endpoint behaviour."""

    def __init__(self, *, reachable=True, unload_status=200, unreachable=False):
        self.reachable = reachable
        self.unload_status = unload_status
        self.unreachable = unreachable
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url))
        if self.unreachable:
            raise gpc.GpuPhaseTransportError("connection refused")
        if url.endswith("/api/tags"):
            return (200 if self.reachable else 503), '{"models": []}'
        if url.endswith("/api/generate"):
            if self.reachable:
                return self.unload_status, '{"done": true, "done_reason": "unload"}'
            return 503, "{}"
        return 404, "{}"

    @property
    def posted_generate(self) -> bool:
        return any(m == "POST" and u.endswith("/api/generate") for m, u in self.calls)


def _fake_runner(used=9800, total=12288, free=2488):
    def run(args, timeout):
        if "--query-gpu=memory.used,memory.total,memory.free" in " ".join(args):
            return 0, f"{used}, {total}, {free}\n", ""
        if "--query-compute-apps" in " ".join(args):
            return 0, "1234, ollama, 9800\n", ""
        return 1, "", "unexpected"

    return run


@pytest.fixture
def nvidia_present(monkeypatch):
    monkeypatch.setattr(gpc.shutil, "which", lambda name: "/usr/bin/nvidia-smi")


@pytest.fixture
def nvidia_missing(monkeypatch):
    monkeypatch.setattr(gpc.shutil, "which", lambda name: None)


def _prepare(**kwargs):
    kwargs.setdefault("release_wait_sec", 0)
    kwargs.setdefault("log", lambda msg: None)
    return gpc.prepare_gpu_for_transcription(**kwargs)


# --- Backend-aware behaviour ---------------------------------------------------


def test_openai_backend_does_not_touch_ollama(nvidia_present):
    http = FakeHttp()
    result = _prepare(
        backend="openai",
        http_client=http,
        command_runner=_fake_runner(),
    )
    assert result.attempted is False
    assert result.action is None
    assert http.calls == []  # never probed or unloaded Ollama


def test_ai_service_backend_attempts_unload(nvidia_present):
    http = FakeHttp(reachable=True, unload_status=200)
    result = _prepare(
        backend="ai_service",
        http_client=http,
        command_runner=_fake_runner(used=1000, total=12288, free=11288),
    )
    assert result.attempted is True
    assert result.ollama_reachable is True
    assert result.action == "ollama_unload_keep_alive_0"
    assert result.action_succeeded is True
    assert http.posted_generate is True


def test_disabled_phase_control_takes_no_action(nvidia_present):
    http = FakeHttp()
    result = _prepare(
        backend="ai_service",
        enabled=False,
        http_client=http,
        command_runner=_fake_runner(),
    )
    assert result.enabled is False
    assert result.attempted is False
    assert http.calls == []


def test_cpu_device_skips_release(nvidia_present):
    http = FakeHttp()
    result = _prepare(
        backend="ai_service",
        whisperx_device="cpu",
        http_client=http,
        command_runner=_fake_runner(),
    )
    assert result.attempted is False
    assert http.calls == []


# --- Failure / safety paths ----------------------------------------------------


def test_ollama_unreachable_is_controlled(nvidia_present):
    http = FakeHttp(unreachable=True)
    result = _prepare(
        backend="ai_service",
        http_client=http,
        command_runner=_fake_runner(),
    )
    assert result.attempted is True
    assert result.ollama_reachable is False
    assert result.action is None  # nothing to unload; not a failure warning
    assert result.warning is None
    assert any("not reachable" in m for m in result.messages)


def test_unload_failure_emits_warning(nvidia_present):
    http = FakeHttp(reachable=True, unload_status=500)
    result = _prepare(
        backend="ai_service",
        whisperx_model="medium",
        http_client=http,
        command_runner=_fake_runner(),
    )
    assert result.action_succeeded is False
    assert result.warning is not None
    assert "out-of-memory" in result.warning.lower() or "cuda" in result.warning.lower()


def test_warning_suppressed_when_warn_disabled(nvidia_present):
    http = FakeHttp(reachable=True, unload_status=500)
    result = _prepare(
        backend="ai_service",
        whisperx_model="medium",
        warn_on_pressure=False,
        http_client=http,
        command_runner=_fake_runner(),
    )
    assert result.action_succeeded is False
    assert result.warning is None


def test_nvidia_missing_does_not_crash(nvidia_missing):
    http = FakeHttp(reachable=True, unload_status=200)
    result = _prepare(
        backend="ai_service",
        whisperx_model="medium",
        http_client=http,
        command_runner=_fake_runner(),
    )
    assert result.gpu_before is None
    assert result.gpu_after is None
    assert result.action_succeeded is True
    # No GPU numbers means no free-VRAM pressure warning, and unload succeeded.
    assert result.warning is None


def test_low_free_vram_after_unload_warns(nvidia_present):
    # Unload "succeeds" but free VRAM is still well below medium's needs.
    http = FakeHttp(reachable=True, unload_status=200)
    result = _prepare(
        backend="ai_service",
        whisperx_model="medium",
        http_client=http,
        command_runner=_fake_runner(used=10000, total=12288, free=2288),
    )
    assert result.action_succeeded is True
    assert result.warning is not None
    assert "VRAM" in result.warning


def test_successful_unload_logs_success(nvidia_present):
    logs: list[str] = []
    http = FakeHttp(reachable=True, unload_status=200)
    result = gpc.prepare_gpu_for_transcription(
        backend="ai_service",
        whisperx_model="small",
        http_client=http,
        command_runner=_fake_runner(used=500, total=12288, free=11788),
        release_wait_sec=0,
        log=logs.append,
    )
    assert result.action_succeeded is True
    assert result.warning is None
    assert any("ok" in line.lower() for line in logs)
    assert result.gpu_before is not None and result.gpu_after is not None


# --- allow_ai_service_selection -----------------------------------------------


def test_allow_selection_openai_is_noop():
    result = gpc.allow_ai_service_selection(backend="openai", log=lambda m: None)
    assert result.attempted is False


def test_allow_selection_ai_service_marks_phase():
    result = gpc.allow_ai_service_selection(backend="ai_service", log=lambda m: None)
    assert result.attempted is True
    assert result.phase == "allow_selection"


# --- keep_alive coercion (Ollama unload uses numeric 0) ------------------------


def test_unload_request_sends_numeric_keep_alive_zero(nvidia_present):
    captured: dict = {}

    def http(method, url, body, timeout):
        if url.endswith("/api/tags"):
            return 200, "{}"
        captured["body"] = body
        return 200, "{}"

    _prepare(backend="ai_service", http_client=http, command_runner=_fake_runner())
    assert captured["body"]["keep_alive"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
