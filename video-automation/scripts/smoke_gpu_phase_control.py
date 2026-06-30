#!/usr/bin/env python3
"""Live, manual smoke check for GPU phase control.

This runs against the REAL machine: real Ollama, real nvidia-smi. It does not
fake anything. Use it to confirm that asking Ollama to release the local model
before WhisperX actually reduces resident VRAM.

Typical use on a GPU box with the local backend selected:

    # 1) make sure the model is resident (run any ai_service clip selection,
    #    or: ollama run qwen2.5:14b-instruct "hi")
    # 2) observe before:
    nvidia-smi
    # 3) run this smoke:
    python3 video-automation/scripts/smoke_gpu_phase_control.py
    # 4) observe after: Qwen VRAM should drop, or a warning is logged.

Exit code is 0 whenever the coordination logic ran without crashing, even if
Ollama is absent or the model could not be released — those are reported, not
faked as success.
"""

from __future__ import annotations

import json
import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import gpu_phase_control as gpc  # noqa: E402
from ai_settings import (  # noqa: E402
    resolve_ai_base_url,
    resolve_ai_model,
    resolve_clip_selection_backend,
    resolve_gpu_phase_control_enabled,
    resolve_warn_on_gpu_pressure,
)


def main() -> int:
    backend = resolve_clip_selection_backend()
    print("== GPU phase control live smoke ==")
    print(f"clip_selection_backend     : {backend}")
    print(f"gpu_phase_control_enabled  : {resolve_gpu_phase_control_enabled()}")
    print(f"warn_on_gpu_pressure       : {resolve_warn_on_gpu_pressure()}")
    print(f"ai_base_url                : {resolve_ai_base_url()}")
    print(f"ai_model                   : {resolve_ai_model()}")
    print(f"nvidia-smi available       : {gpc.nvidia_smi_available()}")

    if backend != "ai_service":
        print(
            "\nNOTE: backend is not ai_service, so prepare_gpu_for_transcription "
            "is a no-op. Set CLIP_SELECTION_BACKEND=ai_service (or choose it in the "
            "Ops UI) to exercise the real release path."
        )

    whisperx_model = os.environ.get("WHISPERX_MODEL") or os.environ.get(
        "MK04_WHISPER_MODEL"
    )

    print("\n-- running prepare_gpu_for_transcription() --")
    result = gpc.prepare_gpu_for_transcription(whisperx_model=whisperx_model)

    print("\n-- result --")
    print(json.dumps(result.as_dict(), indent=2))

    before = result.gpu_before or {}
    after = result.gpu_after or {}
    if before.get("used_mb") is not None and after.get("used_mb") is not None:
        delta = before["used_mb"] - after["used_mb"]
        print(
            f"\nVRAM used before={before['used_mb']}MB after={after['used_mb']}MB "
            f"(released ~{delta}MB)"
        )
        if result.attempted and result.action_succeeded and delta <= 0:
            print(
                "NOTE: unload was acknowledged but resident VRAM did not drop. The "
                "model may not have been resident, or another process holds VRAM."
            )
    if result.warning:
        print(f"\nWARNING: {result.warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
