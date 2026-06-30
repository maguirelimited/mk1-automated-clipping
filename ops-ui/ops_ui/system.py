from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .diagnostics import default_output_funnel_db


STORAGE_USAGE_TTL_SEC = 60.0
_storage_usage_cache: dict[tuple[tuple[str, str], ...], tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str
    returncode: int | None = None


def _run(
    args: list[str],
    *,
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
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


def _scan_path(path: Path) -> tuple[int, int, bool]:
    """Return (total_bytes, file_count, exists) for a path.

    Resilient to files disappearing mid-scan (the retention sweeper races the
    Health page) — every stat/scandir is guarded so a vanished entry is skipped
    rather than crashing the report.
    """
    try:
        if not path.exists():
            return 0, 0, False
    except OSError:
        return 0, 0, False
    try:
        if path.is_file():
            try:
                return path.stat().st_size, 1, True
            except OSError:
                return 0, 0, True
    except OSError:
        return 0, 0, True

    total_bytes = 0
    file_count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total_bytes += entry.stat(follow_symlinks=False).st_size
                    file_count += 1
            except OSError:
                continue
    return total_bytes, file_count, True


def storage_usage(settings: Settings, *, ttl: float = STORAGE_USAGE_TTL_SEC) -> dict[str, Any]:
    """Read-only storage usage broken down by pipeline category.

    Walks each known runtime/log root once and caches the result for ``ttl``
    seconds so the auto-refreshing Health page does not re-walk large trees on
    every load. Missing paths are reported with ``exists`` false rather than
    omitted.
    """
    video_root = settings.runtime_root / "video-automation"
    source_input_root = settings.runtime_root / "source-input"
    categories: list[tuple[str, Path]] = [
        ("Source input videos", video_root / "input"),
        ("Generated clips (public)", video_root / "output"),
        ("Temp / scratch", video_root / "temp"),
        ("Job folders", video_root / "jobs"),
        ("Analytics", video_root / "analytics"),
        ("Source-input temp", source_input_root / "tmp"),
        ("Source-input ready", source_input_root / "inputs" / "ready"),
        ("Source-input rejected", source_input_root / "inputs" / "rejected"),
        ("Source-input state", source_input_root / "state"),
        ("Output-funnel DB", default_output_funnel_db()),
        ("Ops-UI control DB", settings.control_db_path),
        ("Logs", settings.log_root),
    ]

    cache_key = tuple((name, str(path)) for name, path in categories)
    now = time.monotonic()
    cached = _storage_usage_cache.get(cache_key)
    if cached is not None and (now - cached[0]) < ttl:
        return cached[1]

    rows: list[dict[str, Any]] = []
    total_bytes = 0
    total_file_count = 0
    for name, path in categories:
        size, count, exists = _scan_path(path)
        total_bytes += size
        total_file_count += count
        rows.append(
            {
                "category": name,
                "path": str(path),
                "bytes": size,
                "file_count": count,
                "exists": exists,
            }
        )
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    result: dict[str, Any] = {
        "categories": rows,
        "total_bytes": total_bytes,
        "total_file_count": total_file_count,
        "cache_ttl_sec": ttl,
    }
    _storage_usage_cache[cache_key] = (now, result)
    return result


def _age_days(now: float, mtime: float) -> int:
    """Whole 24h periods between ``mtime`` and ``now``.

    Mirrors how GNU ``find`` buckets ``-mtime`` ages (integer 24h periods,
    fractional part discarded).
    """
    delta = now - mtime
    if delta <= 0:
        return 0
    return int(delta // 86400.0)


def _mtime_exceeds(now: float, mtime: float, days: int) -> bool:
    """True when an entry is older than ``find -mtime +days`` would require."""
    return _age_days(now, mtime) > days


def _iter_dir_entries(path: Path) -> list[os.DirEntry[str]]:
    """Immediate children of ``path`` as DirEntry, tolerating races/absence."""
    try:
        return list(os.scandir(path))
    except OSError:
        return []


def _job_fully_aged(job_dir: Path, *, now: float, days: int) -> bool:
    """Whole-folder metadata-TTL test, mirroring sweep_stale_job_dirs().

    A job folder is eligible for whole-folder deletion when nothing inside it
    is newer than the metadata TTL — i.e. every contained file is older than
    ``find -mtime -days``. Keyed off file mtimes (not the dir mtime), matching
    the sweeper. An empty/file-less folder counts as fully aged (the sweeper
    ``find ... -type f -mtime -DAYS`` returns nothing for it too).
    """
    stack = [job_dir]
    while stack:
        current = stack.pop()
        for entry in _iter_dir_entries(current):
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if _age_days(now, entry.stat(follow_symlinks=False).st_mtime) < days:
                        return False
            except OSError:
                continue
    return True


def _job_media(job_dir: Path, *, now: float, media_days: int) -> tuple[int, int, int, int]:
    """Aged per-job media inside one (not-fully-aged) job folder.

    Returns ``(input_copy_bytes, input_copy_files, clip_bytes, clip_files)``
    mirroring two sweeper predicates against ``$JOBS_DIR``:
      * ``-mindepth 2 -maxdepth 2 -type f -name 'input_*' -mtime +MEDIA_DAYS``
      * ``-mindepth 3 -type f -path '*/clips/*' -mtime +MEDIA_DAYS``
    """
    input_bytes = input_files = clip_bytes = clip_files = 0
    # input_* source copies live directly inside the job folder (depth 2).
    for entry in _iter_dir_entries(job_dir):
        try:
            if entry.is_file(follow_symlinks=False) and entry.name.startswith("input_"):
                st = entry.stat(follow_symlinks=False)
                if _mtime_exceeds(now, st.st_mtime, media_days):
                    input_bytes += st.st_size
                    input_files += 1
        except OSError:
            continue
    # clip mirrors: any file living under a ``clips/`` directory in this job.
    stack = [job_dir]
    while stack:
        current = stack.pop()
        for entry in _iter_dir_entries(current):
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                rel_parents = Path(entry.path).relative_to(job_dir).parts[:-1]
                if "clips" not in rel_parents:
                    continue
                st = entry.stat(follow_symlinks=False)
                if _mtime_exceeds(now, st.st_mtime, media_days):
                    clip_bytes += st.st_size
                    clip_files += 1
            except (OSError, ValueError):
                continue
    return input_bytes, input_files, clip_bytes, clip_files


def _aged_depth1(base: Path, *, now: float, media_days: int, files_only: bool) -> tuple[int, int]:
    """Aged entries directly under ``base`` (find -mindepth 1 -maxdepth 1).

    The age test is applied to each top-level entry's own mtime (matching the
    sweeper, which has no ``-type`` filter for output/temp and counts whole
    directory trees via ``du``); directory sizes are summed recursively.
    ``files_only`` mirrors the ``-type f`` predicate used for the input/ root.
    """
    total_bytes = total_files = 0
    for entry in _iter_dir_entries(base):
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            if files_only and is_dir:
                continue
            st = entry.stat(follow_symlinks=False)
            if not _mtime_exceeds(now, st.st_mtime, media_days):
                continue
            if is_dir:
                size, count, _ = _scan_path(Path(entry.path))
                total_bytes += size
                total_files += count
            else:
                total_bytes += st.st_size
                total_files += 1
        except OSError:
            continue
    return total_bytes, total_files


def cleanup_preview(settings: Settings, *, media_days: int, metadata_days: int) -> dict[str, Any]:
    """Structurally preview what the retention sweeper would delete.

    SOURCE OF TRUTH: deploy/scripts/retention-sweeper.sh. The predicates below
    MUST mirror that script per category and they are coupled by maintenance —
    if the sweeper's ``find`` predicates change, update this helper too. We
    compute the estimate in Python because the bash ``--dry-run`` path returns
    before accumulating any would-free total, so its output is not usable here.

    Two TTL tiers:
      * media TTL (``media_days``): per-job ``input_*`` copies, per-job ``clips/*``
        mirrors, ``output/*``, ``temp/*`` and orphan ``input/*`` files, plus the
        source-input ``tmp/*`` (leaked download scratch) and
        ``inputs/rejected/*`` (rejected media + ``.reason.txt`` sidecars) roots.
      * metadata TTL (``metadata_days``): whole job folders, once nothing inside
        is newer than the TTL (job metadata is preserved until then).

    Double-counting: whole-aged job folders are counted in full (every byte,
    incl. their own ``input_*``/``clips``), so the per-job media categories
    deliberately SKIP files inside folders already counted as whole-aged. This
    matches what the real sweep frees (whole-folder removal + media removal in
    surviving folders) without counting the same bytes twice.

    Preserved categories (analytics + source-input state/ledger + source-input
    inputs/ready + db + job metadata) are never walked as deletable, mirroring
    the sweeper's exclusions. Every scandir/stat is guarded so files vanishing
    mid-walk never crash the estimate.
    """
    media_days = int(media_days)
    metadata_days = int(metadata_days)
    now = time.time()

    video_root = settings.runtime_root / "video-automation"
    jobs_dir = video_root / "jobs"
    output_dir = video_root / "output"
    temp_dir = video_root / "temp"
    input_env = os.environ.get("VIDEO_AUTOMATION_INPUT_DIR", "").strip()
    input_dir = Path(input_env) if input_env else (video_root / "input")

    # Source-input roots, derived the same way the sweeper resolves them
    # (env.sh: INPUT_SERVICE_DATA_DIR=$MK04_RUNTIME_ROOT/source-input) and the
    # same way storage_usage() reports them. These are independent of the
    # video-automation roots, so they add categories without double-counting.
    si_data_env = os.environ.get("INPUT_SERVICE_DATA_DIR", "").strip()
    source_input_root = Path(si_data_env) if si_data_env else (settings.runtime_root / "source-input")
    si_tmp_dir = source_input_root / "tmp"
    si_rejected_dir = source_input_root / "inputs" / "rejected"

    # Whole-aged job folders first, so per-job media inside them is not
    # double-counted against the media categories below.
    whole_bytes = whole_files = 0
    input_copy_bytes = input_copy_files = 0
    clip_bytes = clip_files = 0
    for entry in _iter_dir_entries(jobs_dir):
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        job_dir = Path(entry.path)
        if _job_fully_aged(job_dir, now=now, days=metadata_days):
            size, count, _ = _scan_path(job_dir)
            whole_bytes += size
            whole_files += count
            continue
        ib, if_, cb, cf = _job_media(job_dir, now=now, media_days=media_days)
        input_copy_bytes += ib
        input_copy_files += if_
        clip_bytes += cb
        clip_files += cf

    output_bytes, output_files = _aged_depth1(output_dir, now=now, media_days=media_days, files_only=False)
    temp_bytes, temp_files = _aged_depth1(temp_dir, now=now, media_days=media_days, files_only=False)
    input_bytes, input_files = _aged_depth1(input_dir, now=now, media_days=media_days, files_only=True)
    # Source-input tmp/ + inputs/rejected/ mirror the sweeper's depth-1 media-TTL
    # predicate (no -type filter -> whole per-funnel subdirs counted via du).
    si_tmp_bytes, si_tmp_files = _aged_depth1(si_tmp_dir, now=now, media_days=media_days, files_only=False)
    si_rejected_bytes, si_rejected_files = _aged_depth1(
        si_rejected_dir, now=now, media_days=media_days, files_only=False
    )

    categories = [
        {
            "category": "Per-job source copies (input_*)",
            "path": str(jobs_dir),
            "file_count": input_copy_files,
            "bytes": input_copy_bytes,
        },
        {
            "category": "Per-job clip mirrors",
            "path": str(jobs_dir),
            "file_count": clip_files,
            "bytes": clip_bytes,
        },
        {
            "category": "Output clips",
            "path": str(output_dir),
            "file_count": output_files,
            "bytes": output_bytes,
        },
        {
            "category": "Temp/scratch",
            "path": str(temp_dir),
            "file_count": temp_files,
            "bytes": temp_bytes,
        },
        {
            "category": "Orphan input",
            "path": str(input_dir),
            "file_count": input_files,
            "bytes": input_bytes,
        },
        {
            "category": "Source-input temp",
            "path": str(si_tmp_dir),
            "file_count": si_tmp_files,
            "bytes": si_tmp_bytes,
        },
        {
            "category": "Source-input rejected",
            "path": str(si_rejected_dir),
            "file_count": si_rejected_files,
            "bytes": si_rejected_bytes,
        },
        {
            "category": "Whole aged job folders",
            "path": str(jobs_dir),
            "file_count": whole_files,
            "bytes": whole_bytes,
        },
    ]
    total_bytes = sum(row["bytes"] for row in categories)
    total_file_count = sum(row["file_count"] for row in categories)
    return {
        "categories": categories,
        "total_bytes": total_bytes,
        "total_file_count": total_file_count,
        "media_days": media_days,
        "metadata_days": metadata_days,
    }


def _summarize_sweeper_output(message: str) -> str:
    """Pull the sweeper's ``summary removed=.. bytes=..`` tail, if present."""
    for line in reversed(message.splitlines()):
        if "summary removed=" in line:
            return line.split("summary", 1)[1].strip()
    return ""


def run_retention_cleanup(
    settings: Settings,
    *,
    media_days: int,
    metadata_days: int,
    timeout: float = 900.0,
) -> CommandResult:
    """Execute the real cleanup by shelling to deploy/scripts/retention-sweeper.sh.

    Runs the existing sweeper in REAL mode (never ``--dry-run``) so deletion
    logic stays in one place. The script path is derived from
    ``settings.code_root`` (the repo / deployed code root) rather than a
    hardcoded prod path, and the environment (dev/prod) comes from
    ``settings.environment``.

    The sweeper is invoked DIRECTLY as the owning user (no ``sudo``): Ops UI
    runs as ``User=mk04`` which owns the runtime root (``/var/lib/mk04``), so it
    can delete the swept artefacts without elevation, and the documented sudoers
    grant only covers ``systemctl`` anyway. Running without sudo also means the
    ``MK04_RUNTIME_ROOT``/``MK04_ENV`` env we set below propagates reliably to
    the script (sudo would strip it), so execution targets exactly the tree the
    preview walked.
    """
    media_days = int(media_days)
    metadata_days = int(metadata_days)
    if media_days < 1 or metadata_days < 1:
        return CommandResult(False, "media_days and metadata_days must both be >= 1")

    env_name = settings.environment if settings.environment in {"dev", "prod"} else "dev"
    script = settings.code_root / "deploy" / "scripts" / "retention-sweeper.sh"
    try:
        script_exists = script.is_file()
    except OSError:
        script_exists = False
    if not script_exists:
        return CommandResult(False, f"retention-sweeper.sh not found at {script}")

    command = [
        "bash",
        str(script),
        env_name,
        "--media-days",
        str(media_days),
        "--days",
        str(metadata_days),
    ]

    # Pass the same runtime root the preview walked so execution targets the
    # identical tree. With sudo removed, this env propagates reliably to the
    # script.
    sweep_env = dict(os.environ)
    sweep_env["MK04_ENV"] = env_name
    sweep_env.setdefault("MK04_RUNTIME_ROOT", str(settings.runtime_root))
    return _run(command, timeout=timeout, env=sweep_env)


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

