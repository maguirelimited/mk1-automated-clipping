"""MK1 GPU phase control for the clipping pipeline.

WhisperX transcription and the local LLM (Qwen via Ollama, used by the
``ai-service`` clip-selection backend) are both heavy GPU phases. On a ~12GB
card the 14B model can occupy most of the VRAM, so loading WhisperX while the
model is resident risks CUDA out-of-memory.

This module makes the two heavy GPU phases explicit and sequential:

    before WhisperX transcription:
        prepare_gpu_for_transcription()   # ask Ollama to release the model
    run WhisperX transcription
    before local clip selection:
        allow_ai_service_selection()      # log marker; Ollama reloads lazily

It does NOT execute the pipeline, own job truth, queue work, kill processes,
restart services, or switch backends. It only nudges Ollama to free VRAM using
the supported ``keep_alive=0`` unload, and reports what it observed.

Design rules:
- Only act when the resolved clip-selection backend is ``ai_service``. The
  OpenAI path is never disturbed.
- Be safe when Ollama is missing/unreachable, ``nvidia-smi`` is absent, or
  WhisperX runs on CPU. None of these crash the pipeline.
- Prefer the least disruptive option: the supported Ollama ``keep_alive=0``
  unload. Never kill the Ollama process or random GPU processes.
- Never block forever: every probe and request uses a short timeout.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

try:  # Resolution helpers live alongside this module in scripts/.
    from ai_settings import (
        resolve_ai_base_url,
        resolve_ai_model,
        resolve_clip_selection_backend,
        resolve_gpu_phase_control_enabled,
        resolve_warn_on_gpu_pressure,
    )
except Exception:  # pragma: no cover - extremely defensive import fallback
    resolve_ai_base_url = None  # type: ignore[assignment]
    resolve_ai_model = None  # type: ignore[assignment]
    resolve_clip_selection_backend = None  # type: ignore[assignment]
    resolve_gpu_phase_control_enabled = None  # type: ignore[assignment]
    resolve_warn_on_gpu_pressure = None  # type: ignore[assignment]


# A command runner takes (args, timeout_sec) and returns (returncode, stdout, stderr).
CommandRunner = Callable[[list[str], float], "tuple[int, str, str]"]
# An HTTP poster takes (method, url, body_or_none, timeout_sec) and returns
# (status_code, body_text). It must raise GpuPhaseTransportError on transport
# failure (connection refused, timeout, DNS, etc.).
HttpClient = Callable[..., "tuple[int, str]"]

# Approximate minimum free VRAM (MB) WhisperX wants per model size. These are
# deliberately conservative ballparks used only to decide whether to WARN; they
# never block transcription.
_WHISPERX_MIN_FREE_MB: dict[str, int] = {
    "tiny": 1500,
    "base": 1500,
    "small": 2500,
    "medium": 5000,
    "large": 10000,
    "large-v1": 10000,
    "large-v2": 10000,
    "large-v3": 10000,
}
_WHISPERX_DEFAULT_MIN_FREE_MB = 5000

DEFAULT_PROBE_TIMEOUT_SEC = 3.0
DEFAULT_UNLOAD_TIMEOUT_SEC = 15.0
DEFAULT_RELEASE_WAIT_SEC = 1.5


class GpuPhaseTransportError(RuntimeError):
    """Raised by an HTTP client when Ollama cannot be reached at all."""


@dataclass
class GpuPhaseResult:
    """Structured, log-friendly outcome of one GPU phase transition."""

    phase: str
    backend: str = ""
    enabled: bool = True
    attempted: bool = False
    ollama_reachable: bool | None = None
    action: str | None = None
    action_succeeded: bool | None = None
    gpu_before: dict[str, Any] | None = None
    gpu_after: dict[str, Any] | None = None
    warning: str | None = None
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "backend": self.backend,
            "enabled": self.enabled,
            "attempted": self.attempted,
            "ollama_reachable": self.ollama_reachable,
            "action": self.action,
            "action_succeeded": self.action_succeeded,
            "gpu_before": self.gpu_before,
            "gpu_after": self.gpu_after,
            "warning": self.warning,
            "messages": list(self.messages),
        }


def _default_log(message: str) -> None:
    print(f"[gpu-phase] {message}", file=sys.stderr, flush=True)


def _default_command_runner(args: list[str], timeout_sec: float) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_sec)
    return proc.returncode, proc.stdout, proc.stderr


def _default_http_client(
    method: str, url: str, body: dict[str, Any] | None, timeout_sec: float
) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return int(exc.code), text
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise GpuPhaseTransportError(str(reason)) from exc


def nvidia_smi_available(runner: CommandRunner | None = None) -> bool:
    """True only if the ``nvidia-smi`` binary exists on PATH."""
    return shutil.which("nvidia-smi") is not None


def read_gpu_memory(
    *,
    runner: CommandRunner | None = None,
    timeout_sec: float = DEFAULT_PROBE_TIMEOUT_SEC,
    include_processes: bool = True,
) -> dict[str, Any] | None:
    """Return simple VRAM numbers via ``nvidia-smi``, or None if unavailable.

    Never raises. Output shape (primary numbers are GPU 0)::

        {"used_mb": int, "total_mb": int, "free_mb": int,
         "gpus": [{"index", "used_mb", "total_mb", "free_mb"}, ...],
         "processes": [{"pid", "name", "used_mb"}, ...]}
    """
    if shutil.which("nvidia-smi") is None:
        return None
    run = runner or _default_command_runner
    try:
        code, out, _ = run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            timeout_sec,
        )
    except Exception:
        return None
    if code != 0:
        return None

    gpus: list[dict[str, Any]] = []
    for idx, line in enumerate(str(out or "").strip().splitlines()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            used = int(float(parts[0]))
            total = int(float(parts[1]))
            free = int(float(parts[2]))
        except (TypeError, ValueError):
            continue
        gpus.append({"index": idx, "used_mb": used, "total_mb": total, "free_mb": free})
    if not gpus:
        return None

    result: dict[str, Any] = {
        "used_mb": gpus[0]["used_mb"],
        "total_mb": gpus[0]["total_mb"],
        "free_mb": gpus[0]["free_mb"],
        "gpus": gpus,
    }
    if include_processes:
        result["processes"] = _read_gpu_processes(run, timeout_sec)
    return result


def _read_gpu_processes(run: CommandRunner, timeout_sec: float) -> list[dict[str, Any]]:
    try:
        code, out, _ = run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout_sec,
        )
    except Exception:
        return []
    if code != 0:
        return []
    procs: list[dict[str, Any]] = []
    for line in str(out or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid = int(float(parts[0]))
        except (TypeError, ValueError):
            pid = None
        try:
            used = int(float(parts[2]))
        except (TypeError, ValueError):
            used = None
        procs.append({"pid": pid, "name": parts[1], "used_mb": used})
    return procs


def _ollama_reachable(
    base_url: str, http_client: HttpClient, timeout_sec: float
) -> bool:
    url = base_url.rstrip("/") + "/api/tags"
    try:
        status, _ = http_client("GET", url, None, timeout_sec)
    except GpuPhaseTransportError:
        return False
    except Exception:
        return False
    return 200 <= int(status) < 300


def _ollama_unload(
    base_url: str, model: str, http_client: HttpClient, timeout_sec: float
) -> tuple[bool, str]:
    """Ask Ollama to release ``model`` from memory using the supported
    ``keep_alive=0`` request. Returns (ok, detail). Never raises.
    """
    url = base_url.rstrip("/") + "/api/generate"
    body = {"model": model, "keep_alive": 0, "stream": False}
    try:
        status, text = http_client("POST", url, body, timeout_sec)
    except GpuPhaseTransportError as exc:
        return False, f"ollama unreachable during unload: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"unexpected unload error: {exc!r}"
    if not (200 <= int(status) < 300):
        snippet = (text or "").strip()[:200]
        return False, f"ollama unload returned HTTP {status}: {snippet}"
    return True, "ollama acknowledged keep_alive=0 unload"


def _normalize_model_name(model: str | None) -> str:
    name = (model or "").strip().lower()
    # Strip an Ollama-style tag (":...") so "medium" / "large-v3" keys still match
    # whisper-style names, while leaving WhisperX model names untouched.
    return name


def _min_free_mb_for(whisperx_model: str | None) -> int:
    name = _normalize_model_name(whisperx_model)
    return _WHISPERX_MIN_FREE_MB.get(name, _WHISPERX_DEFAULT_MIN_FREE_MB)


def _pressure_warning(
    *,
    warn_enabled: bool,
    action_succeeded: bool | None,
    gpu_after: dict[str, Any] | None,
    whisperx_model: str | None,
    ollama_reachable: bool | None,
) -> str | None:
    if not warn_enabled:
        return None
    model_hint = (whisperx_model or "the configured WhisperX model").strip()
    if action_succeeded is False:
        return (
            "Could not release the local model from GPU before transcription. "
            f"WhisperX ({model_hint}) may hit CUDA out-of-memory. Consider setting "
            "WHISPERX_MODEL=small (or tiny) or WHISPERX_DEVICE=cpu for this machine."
        )
    if gpu_after is not None:
        free_mb = gpu_after.get("free_mb")
        need_mb = _min_free_mb_for(whisperx_model)
        if isinstance(free_mb, int) and free_mb < need_mb:
            return (
                f"GPU free VRAM is {free_mb}MB after releasing the local model, "
                f"below the ~{need_mb}MB WhisperX '{model_hint}' typically needs. "
                "Consider WHISPERX_MODEL=small/tiny or WHISPERX_DEVICE=cpu to avoid "
                "CUDA out-of-memory."
            )
    return None


def _resolve_backend(backend: str | None) -> str:
    if backend:
        return backend
    if resolve_clip_selection_backend is not None:
        try:
            return resolve_clip_selection_backend()
        except Exception:
            return "openai"
    return "openai"


def prepare_gpu_for_transcription(
    *,
    backend: str | None = None,
    whisperx_model: str | None = None,
    whisperx_device: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    enabled: bool | None = None,
    warn_on_pressure: bool | None = None,
    http_client: HttpClient | None = None,
    command_runner: CommandRunner | None = None,
    probe_timeout_sec: float = DEFAULT_PROBE_TIMEOUT_SEC,
    unload_timeout_sec: float = DEFAULT_UNLOAD_TIMEOUT_SEC,
    release_wait_sec: float = DEFAULT_RELEASE_WAIT_SEC,
    log: Callable[[str], None] | None = None,
) -> GpuPhaseResult:
    """Release the local LLM from GPU before WhisperX runs, when relevant.

    Only acts when the resolved clip-selection backend is ``ai_service`` and
    phase control is enabled. Safe to call unconditionally: for the OpenAI path,
    a disabled flag, CPU transcription, or an absent/unreachable Ollama it logs
    and returns without touching the pipeline.
    """
    emit = log or _default_log
    resolved_backend = _resolve_backend(backend)
    result = GpuPhaseResult(phase="prepare_transcription", backend=resolved_backend)

    if resolved_backend != "ai_service":
        result.messages.append(
            f"backend={resolved_backend}: no local-AI GPU preparation needed."
        )
        emit(result.messages[-1])
        return result

    if enabled is None:
        enabled = (
            resolve_gpu_phase_control_enabled()
            if resolve_gpu_phase_control_enabled is not None
            else True
        )
    result.enabled = bool(enabled)
    if not enabled:
        result.messages.append(
            "LOCAL_AI_GPU_PHASE_CONTROL_ENABLED is off: skipping local-model GPU release."
        )
        emit(result.messages[-1])
        return result

    if (whisperx_device or "").strip().lower() == "cpu":
        result.messages.append(
            "WhisperX device=cpu: transcription does not use the GPU, skipping release."
        )
        emit(result.messages[-1])
        return result

    if warn_on_pressure is None:
        warn_on_pressure = (
            resolve_warn_on_gpu_pressure()
            if resolve_warn_on_gpu_pressure is not None
            else True
        )

    if base_url is None:
        base_url = (
            resolve_ai_base_url()
            if resolve_ai_base_url is not None
            else "http://localhost:11434"
        )
    if model is None:
        model = (
            resolve_ai_model() if resolve_ai_model is not None else "qwen2.5:14b-instruct"
        )

    client = http_client or _default_http_client
    result.attempted = True
    emit(
        f"backend=ai_service: preparing GPU for WhisperX "
        f"(model_to_release={model} ollama={base_url} whisperx_model={whisperx_model})."
    )

    result.gpu_before = read_gpu_memory(runner=command_runner, timeout_sec=probe_timeout_sec)
    if result.gpu_before is not None:
        emit(
            "GPU before: "
            f"used={result.gpu_before.get('used_mb')}MB "
            f"free={result.gpu_before.get('free_mb')}MB "
            f"total={result.gpu_before.get('total_mb')}MB"
        )
    else:
        emit(
            "GPU memory numbers unavailable (nvidia-smi missing or driver "
            "unreachable); continuing."
        )

    reachable = _ollama_reachable(base_url, client, probe_timeout_sec)
    result.ollama_reachable = reachable
    if not reachable:
        result.messages.append(
            f"Ollama not reachable at {base_url}: nothing to release "
            "(local model is not resident here)."
        )
        emit(result.messages[-1])
        return result

    result.action = "ollama_unload_keep_alive_0"
    ok, detail = _ollama_unload(base_url, model, client, unload_timeout_sec)
    result.action_succeeded = ok
    result.messages.append(detail)
    emit(f"unload action ({result.action}): {'ok' if ok else 'FAILED'} — {detail}")

    if ok and release_wait_sec > 0:
        time.sleep(release_wait_sec)

    result.gpu_after = read_gpu_memory(runner=command_runner, timeout_sec=probe_timeout_sec)
    if result.gpu_after is not None:
        emit(
            "GPU after: "
            f"used={result.gpu_after.get('used_mb')}MB "
            f"free={result.gpu_after.get('free_mb')}MB "
            f"total={result.gpu_after.get('total_mb')}MB"
        )

    result.warning = _pressure_warning(
        warn_enabled=bool(warn_on_pressure),
        action_succeeded=result.action_succeeded,
        gpu_after=result.gpu_after,
        whisperx_model=whisperx_model,
        ollama_reachable=reachable,
    )
    if result.warning:
        emit(f"WARNING: {result.warning}")
    return result


def allow_ai_service_selection(
    *,
    backend: str | None = None,
    enabled: bool | None = None,
    command_runner: CommandRunner | None = None,
    probe_timeout_sec: float = DEFAULT_PROBE_TIMEOUT_SEC,
    read_gpu: bool = False,
    log: Callable[[str], None] | None = None,
) -> GpuPhaseResult:
    """Mark the transition into the local clip-selection phase.

    Transcription has finished and released the GPU. The local model is allowed
    to load again on the next ``ai-service`` request (Ollama loads lazily), so
    this is primarily an observability marker. It performs no GPU action and is
    a no-op for the OpenAI backend.
    """
    emit = log or _default_log
    resolved_backend = _resolve_backend(backend)
    result = GpuPhaseResult(phase="allow_selection", backend=resolved_backend)

    if resolved_backend != "ai_service":
        result.messages.append(
            f"backend={resolved_backend}: local model not used for selection."
        )
        return result

    if enabled is None:
        enabled = (
            resolve_gpu_phase_control_enabled()
            if resolve_gpu_phase_control_enabled is not None
            else True
        )
    result.enabled = bool(enabled)
    result.attempted = True
    result.messages.append(
        "Transcription GPU phase complete: local model may load again for clip selection."
    )
    emit(result.messages[-1])
    if read_gpu:
        result.gpu_before = read_gpu_memory(
            runner=command_runner, timeout_sec=probe_timeout_sec
        )
    return result
