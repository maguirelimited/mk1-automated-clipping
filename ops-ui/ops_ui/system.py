from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str
    returncode: int | None = None


def _run(args: list[str], *, timeout: float = 5.0) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return CommandResult(False, f"Command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(False, f"Command timed out: {' '.join(args)}")
    message = (completed.stdout or completed.stderr or "").strip()
    return CommandResult(completed.returncode == 0, message, completed.returncode)


def systemd_available() -> bool:
    return shutil.which("systemctl") is not None


def service_status(unit: str) -> dict[str, Any]:
    if not systemd_available():
        return {
            "unit": unit,
            "available": False,
            "active_state": "unknown",
            "sub_state": "systemctl unavailable",
            "description": "systemd controls are available on Ubuntu deployments",
        }

    result = _run(
        [
            "systemctl",
            "show",
            unit,
            "--property=Id,LoadState,ActiveState,SubState,Description",
            "--no-pager",
        ],
        timeout=3.0,
    )
    status = {
        "unit": unit,
        "available": result.ok,
        "active_state": "unknown",
        "sub_state": "",
        "description": "",
        "load_state": "",
    }
    if not result.ok:
        status["sub_state"] = result.message or "systemctl status failed"
        return status
    for line in result.message.splitlines():
        key, _, value = line.partition("=")
        if key == "LoadState":
            status["load_state"] = value
        elif key == "ActiveState":
            status["active_state"] = value
        elif key == "SubState":
            status["sub_state"] = value
        elif key == "Description":
            status["description"] = value
    return status


def service_action(unit: str, action: str) -> CommandResult:
    if action not in {"start", "stop", "restart"}:
        return CommandResult(False, f"Unsupported service action: {action}")
    if not systemd_available():
        return CommandResult(False, "systemctl is not available on this machine")
    command = ["systemctl", action, unit]
    if os.geteuid() != 0:
        command.insert(0, "sudo")
    return _run(command, timeout=30.0)


def journal_logs(unit: str, lines: int) -> str:
    if shutil.which("journalctl") is None:
        return "journalctl is not available on this machine."
    result = _run(
        ["journalctl", "-u", unit, "-n", str(max(1, min(lines, 300))), "--no-pager", "--output=short-iso"],
        timeout=5.0,
    )
    return result.message or "No recent journal entries."


def machine_stats() -> dict[str, Any]:
    usage = shutil.disk_usage(Path.cwd())
    load_avg = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)
    stats: dict[str, Any] = {
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "load_avg": load_avg,
        "memory": _memory_stats(),
        "disk": {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round((usage.used / usage.total) * 100, 1) if usage.total else None,
        },
        "gpu": _gpu_stats(),
    }
    return stats


def _memory_stats() -> dict[str, Any]:
    if platform.system() == "Darwin":
        vm = _run(["vm_stat"], timeout=2.0)
        page_size = 4096
        out: dict[str, int] = {}
        if vm.ok:
            for line in vm.message.splitlines():
                key, _, raw = line.partition(":")
                raw = raw.strip().rstrip(".")
                if raw.isdigit():
                    out[key] = int(raw) * page_size
        total_result = _run(["sysctl", "-n", "hw.memsize"], timeout=2.0)
        total = int(total_result.message) if total_result.ok and total_result.message.isdigit() else None
        free = out.get("Pages free", 0) + out.get("Pages inactive", 0)
        used = total - free if total is not None else None
        return _percent_payload(total=total, used=used, free=free if total is not None else None)

    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return {"total": None, "used": None, "free": None, "percent": None}
    values: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.strip().split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    used = total - available if total is not None and available is not None else None
    return _percent_payload(total=total, used=used, free=available)


def _percent_payload(*, total: int | None, used: int | None, free: int | None) -> dict[str, Any]:
    percent = round((used / total) * 100, 1) if total and used is not None else None
    return {"total": total, "used": used, "free": free, "percent": percent}


def _gpu_stats() -> dict[str, Any]:
    if shutil.which("nvidia-smi") is None:
        return {"available": False, "summary": "nvidia-smi not found"}
    result = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=3.0,
    )
    if not result.ok:
        return {"available": False, "summary": result.message}
    gpus = []
    for line in result.message.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 5:
            gpus.append(
                {
                    "name": parts[0],
                    "utilization_percent": parts[1],
                    "memory_used_mb": parts[2],
                    "memory_total_mb": parts[3],
                    "temperature_c": parts[4],
                }
            )
    return {"available": True, "gpus": gpus}

