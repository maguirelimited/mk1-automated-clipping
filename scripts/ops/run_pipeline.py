#!/usr/bin/env python3
"""Shared production pipeline entrypoint (Reliability & Recovery + Prompt 4 gate).

Invoked by scripts/ops/run-pipeline.sh. All trigger sources (scheduler, manual
CLI, remote SSH, Operations UI) must use this path.

Responsibilities:
  - explicit environment
  - config validation
  - boot readiness (abort if NOT READY)
  - environment-specific execution lock (same-env overlap prevention)
  - cross-environment execution gate (production priority + heavy-work correlation)
  - run records (canonical execution history)
  - invoke POST /run-funnel, wait for video jobs to reach terminal status
  - SUCCESS only after required jobs and handoff finish
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from boot_verification import build_boot_verification  # noqa: E402
from execution_gate import (  # noqa: E402
    AdmissionHandle,
    GateError,
    admit_orchestration,
    read_gate_status,
)
from execution_lock import (  # noqa: E402
    acquire_lock,
    build_lock_payload,
    release_lock,
)
from ops_readonly import (  # noqa: E402
    REPO_ROOT,
    canonical_env,
    ensure_config_scripts_on_path,
    env_label,
    mk04_env,
    scheduled_runs_allowed,
    service_health_urls,
)
from run_records import (  # noqa: E402
    STATUS_FAIL,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    create_running_record,
    ensure_terminal,
    finalize_record,
    resolve_code_commit,
    runs_root_for_env,
    write_config_snapshot,
    write_terminal_record,
)

ensure_config_scripts_on_path()
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from config_manager import ConfigError, ConfigManager  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402
from storage.disk_pressure import can_start_new_job, record_disk_pressure_block  # noqa: E402

ALLOWED_TRIGGERS = frozenset(
    {"scheduled", "manual_cli", "operations_ui", "remote_ssh", "test"}
)

EXIT_SUCCESS = 0
EXIT_PIPELINE_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_NOT_READY = 4
EXIT_LOCK_HELD = 5
EXIT_GATE_REFUSED = 6

DEFAULT_RUN_FUNNEL_HTTP_TIMEOUT_SEC = 900.0
DEFAULT_JOB_WAIT_TIMEOUT_SEC = 7200.0
DEFAULT_JOB_POLL_INTERVAL_SEC = 5.0


def run_funnel_http_timeout_sec() -> float:
    """HTTP wait for source-input /run-funnel (all funnels).

    Override with ``RUN_FUNNEL_HTTP_TIMEOUT_SEC``, or fall back to
    ``OPS_UI_FUNNEL_RUN_TIMEOUT_SEC`` when set. Default is 900s.
    """
    for key in ("RUN_FUNNEL_HTTP_TIMEOUT_SEC", "OPS_UI_FUNNEL_RUN_TIMEOUT_SEC"):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return DEFAULT_RUN_FUNNEL_HTTP_TIMEOUT_SEC


@dataclass
class PipelineRunContext:
    environment: str
    env_label: str
    funnel_id: str
    trigger: str
    run_id: str
    run_dir: Path
    log_path: Path
    lock_acquired: bool = False
    lock_pid: int | None = None
    record_started: bool = False
    code_commit: str | None = None
    config_snapshot_path: str | None = None
    resolved_config: Any | None = None
    admission: AdmissionHandle | None = None
    job_ids: list[str] = field(default_factory=list)


def _utc_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _log(stream: TextIO, message: str) -> None:
    line = message if message.endswith("\n") else message + "\n"
    stream.write(line)
    stream.flush()
    sys.stdout.write(line)
    sys.stdout.flush()


def validate_config(mk04_env_token: str) -> tuple[int, str, Any | None]:
    """Return (exit_code_or_0, detail, resolved_config)."""
    try:
        canonical = canonical_env(mk04_env_token)
    except ValueError as exc:
        return EXIT_USAGE, str(exc), None
    try:
        resolved = ConfigManager.load(
            environment=canonical,
            config_root=REPO_ROOT / "config",
        )
    except ConfigError as exc:
        return EXIT_CONFIG, f"config validation failed: {exc}", None
    return EXIT_SUCCESS, "config validation passed", resolved


def check_boot_readiness(mk04_env_token: str) -> tuple[int, str]:
    report = build_boot_verification(mk04_env_token)
    if report.overall == "NOT READY":
        failures = [
            f"{c.label}={c.result}"
            for c in report.components
            if c.required and c.result == "FAIL"
        ]
        detail = "; ".join(failures) or "required components not ready"
        return EXIT_NOT_READY, f"boot readiness NOT READY ({detail})"
    return EXIT_SUCCESS, f"boot readiness {report.overall}"


def check_scheduled_runtime_gate(mk04_env_token: str, trigger: str) -> tuple[str, str]:
    """For trigger=scheduled only: respect stop-scheduler / start-scheduler control.

    Canonical control plane writes data/<env>/control_state.json via
    scripts/ops/stop-scheduler.sh and start-scheduler.sh. Readiness and locks
    are enforced separately in this entrypoint — not by the control scripts.
    """
    if trigger != "scheduled":
        return "proceed", "non-scheduled trigger; scheduler runtime gate not applied"

    try:
        canonical = canonical_env(mk04_env_token)
        data_root = EnvironmentStatePaths.from_resolved_config(
            ConfigManager.load(
                environment=canonical,
                config_root=REPO_ROOT / "config",
            )
        ).data_root
    except Exception:
        data_root = REPO_ROOT / "data" / mk04_env(canonical_env(mk04_env_token))

    allowed, detail = scheduled_runs_allowed(data_root)
    if not allowed:
        return "skip", f"scheduled run skipped: {detail}"
    return "proceed", detail


def acquire_execution_lock(ctx: PipelineRunContext) -> tuple[bool, str]:
    payload = build_lock_payload(
        environment=ctx.environment,
        run_id=ctx.run_id,
        trigger=ctx.trigger,
        funnel_id=ctx.funnel_id,
    )
    ok, detail, _blocking = acquire_lock(ctx.environment, payload)
    if ok:
        ctx.lock_acquired = True
        ctx.lock_pid = payload.pid
    return ok, detail


def release_execution_lock(ctx: PipelineRunContext) -> str:
    if not ctx.lock_acquired:
        return "execution lock was not held by this run"
    ok, detail = release_lock(
        ctx.environment,
        run_id=ctx.run_id,
        pid=ctx.lock_pid,
    )
    if ok:
        ctx.lock_acquired = False
    return detail


def prepare_run_context(
    mk04_env_token: str,
    *,
    funnel_id: str,
    trigger: str,
) -> PipelineRunContext:
    token = mk04_env(canonical_env(mk04_env_token))
    run_id = f"run_{_utc_stamp()}_{trigger}"
    run_dir = runs_root_for_env(token) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"
    return PipelineRunContext(
        environment=token,
        env_label=env_label(canonical_env(mk04_env_token)),
        funnel_id=funnel_id,
        trigger=trigger,
        run_id=run_id,
        run_dir=run_dir,
        log_path=log_path,
        lock_acquired=False,
        lock_pid=None,
        record_started=False,
        code_commit=resolve_code_commit(),
    )


def _record_terminal(
    ctx: PipelineRunContext,
    *,
    status: str,
    exit_code: int,
    failure_reason: str | None = None,
    detail: str | None = None,
    jobs_started: int = 0,
    jobs_completed: int = 0,
    jobs_failed: int = 0,
) -> None:
    if ctx.record_started:
        finalize_record(
            ctx.run_dir,
            status=status,
            exit_code=exit_code,
            failure_reason=failure_reason,
            detail=detail,
            jobs_started=jobs_started,
            jobs_completed=jobs_completed,
            jobs_failed=jobs_failed,
        )
    else:
        write_terminal_record(
            run_dir=ctx.run_dir,
            run_id=ctx.run_id,
            environment=ctx.environment,
            trigger=ctx.trigger,
            funnel_id=ctx.funnel_id,
            log_path=ctx.log_path,
            status=status,
            exit_code=exit_code,
            failure_reason=failure_reason,
            detail=detail,
            jobs_started=jobs_started,
            jobs_completed=jobs_completed,
            jobs_failed=jobs_failed,
            code_commit=ctx.code_commit,
            config_snapshot_path=ctx.config_snapshot_path,
        )
    ctx.record_started = True


def _start_running_record(ctx: PipelineRunContext) -> None:
    if ctx.resolved_config is not None and not ctx.config_snapshot_path:
        ctx.config_snapshot_path = write_config_snapshot(ctx.run_dir, ctx.resolved_config)
    create_running_record(
        run_dir=ctx.run_dir,
        run_id=ctx.run_id,
        environment=ctx.environment,
        trigger=ctx.trigger,
        funnel_id=ctx.funnel_id,
        log_path=ctx.log_path,
        code_commit=ctx.code_commit,
        config_snapshot_path=ctx.config_snapshot_path,
    )
    ctx.record_started = True


def _api_base_url(mk04_env_token: str) -> str:
    health_url = service_health_urls(mk04_env_token)["API"]
    if health_url.endswith("/healthz"):
        return health_url[: -len("/healthz")]
    return health_url.rstrip("/")


def invoke_run_funnel(
    mk04_env_token: str,
    funnel_id: str,
    log: TextIO,
    *,
    run_id: str,
    environment: str,
) -> tuple[int, str, str, list[str]]:
    """Invoke POST /run-funnel. Returns (exit_code, detail, pipeline_status, job_ids)."""
    url = f"{_api_base_url(mk04_env_token)}/run-funnel"
    payload = json.dumps(
        {
            "funnel_id": funnel_id,
            "orchestration_context": {
                "run_id": run_id,
                "environment": environment,
                "trigger": "run_pipeline",
            },
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-MK04-Run-Id": run_id,
        "X-MK04-Environment": environment,
    }
    secret = (os.environ.get("INPUT_SERVICE_SECRET") or "").strip()
    if secret:
        headers["X-Input-Service-Secret"] = secret

    timeout_sec = run_funnel_http_timeout_sec()
    _log(log, f"invoke POST {url} funnel_id={funnel_id} timeout_sec={timeout_sec:g}")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        _log(log, f"FAIL run-funnel HTTP {exc.code}: {body[:500]}")
        return EXIT_PIPELINE_FAIL, f"run-funnel HTTP {exc.code}", "", []
    except Exception as exc:
        _log(log, f"FAIL run-funnel request error: {exc}")
        return EXIT_PIPELINE_FAIL, f"run-funnel request error: {exc}", "", []

    _log(log, f"response HTTP {status_code}: {body}")
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return EXIT_PIPELINE_FAIL, "run-funnel returned non-JSON body", "", []

    status = str((data or {}).get("status") or "")
    job_ids: list[str] = []
    clipping = data.get("clipping_job") if isinstance(data.get("clipping_job"), dict) else {}
    jid = str(clipping.get("job_id") or data.get("job_id") or "").strip()
    if jid:
        job_ids.append(jid)
    for item in data.get("job_ids") or []:
        text = str(item or "").strip()
        if text and text not in job_ids:
            job_ids.append(text)
    _log(
        log,
        f"result funnel_id={funnel_id} status={status or 'unknown'} job_ids={job_ids}",
    )
    if status in {"input_ready", "no_input_available"}:
        return EXIT_SUCCESS, f"pipeline status={status}", status, job_ids
    return EXIT_PIPELINE_FAIL, f"unexpected pipeline status={status or 'unknown'}", status, job_ids


def _video_automation_base_url(mk04_env_token: str) -> str:
    urls = service_health_urls(mk04_env_token)
    health = urls.get("Worker") or urls.get("video-automation") or ""
    if health.endswith("/healthz"):
        return health[: -len("/healthz")]
    override = (os.environ.get("VIDEO_AUTOMATION_BASE_URL") or "").strip()
    if override:
        return override.rstrip("/")
    host = os.environ.get("VIDEO_AUTOMATION_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("VIDEO_AUTOMATION_PORT", "5050").strip() or "5050"
    return f"http://{host}:{port}"


def _job_wait_timeout_sec() -> float:
    raw = (os.environ.get("MK04_JOB_WAIT_TIMEOUT_SEC") or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_JOB_WAIT_TIMEOUT_SEC


def _job_poll_interval_sec() -> float:
    raw = (os.environ.get("MK04_JOB_POLL_INTERVAL_SEC") or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_JOB_POLL_INTERVAL_SEC


def wait_for_video_jobs(
    mk04_env_token: str,
    job_ids: list[str],
    log: TextIO,
) -> tuple[int, str, int, int, int]:
    """
    Poll video-automation until each job is terminal.

    Returns (exit_code, detail, jobs_started, jobs_completed, jobs_failed).
    """
    if not job_ids:
        return EXIT_SUCCESS, "no video jobs to wait for", 0, 0, 0

    base = _video_automation_base_url(mk04_env_token)
    timeout_sec = _job_wait_timeout_sec()
    interval = _job_poll_interval_sec()
    deadline = time.monotonic() + timeout_sec
    remaining = set(job_ids)
    completed = 0
    failed = 0
    headers = {"Accept": "application/json"}
    secret = (os.environ.get("VIDEO_AUTOMATION_SECRET") or "").strip()
    if secret:
        headers["X-Video-Automation-Secret"] = secret

    _log(log, f"waiting for {len(job_ids)} video job(s): {job_ids}")
    while remaining:
        if time.monotonic() > deadline:
            return (
                EXIT_PIPELINE_FAIL,
                f"timed out waiting for jobs: {sorted(remaining)}",
                len(job_ids),
                completed,
                failed + len(remaining),
            )
        for job_id in list(remaining):
            url = f"{base}/jobs/{job_id}"
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    data = json.loads(body) if body else {}
            except Exception as exc:
                _log(log, f"job poll error job_id={job_id}: {exc}")
                time.sleep(interval)
                continue
            status = str((data or {}).get("status") or "").lower()
            if status in {"success", "failed"}:
                remaining.discard(job_id)
                if status == "success":
                    completed += 1
                    _log(log, f"job terminal success job_id={job_id}")
                else:
                    failed += 1
                    err = str((data or {}).get("error") or "failed")
                    _log(log, f"job terminal failed job_id={job_id} error={err}")
        if remaining:
            time.sleep(interval)

    if failed:
        return (
            EXIT_PIPELINE_FAIL,
            f"video jobs failed={failed} completed={completed}",
            len(job_ids),
            completed,
            failed,
        )
    return (
        EXIT_SUCCESS,
        f"video jobs completed={completed}",
        len(job_ids),
        completed,
        0,
    )


def _print_outcome(ctx: PipelineRunContext, status: str) -> None:
    print(f"run_id={ctx.run_id}")
    print(f"log_path={ctx.log_path}")
    print(f"record_path={ctx.run_dir / 'run_record.json'}")
    print(f"status={status}")


def run_pipeline(
    mk04_env_token: str,
    *,
    funnel_id: str,
    trigger: str,
) -> int:
    if trigger not in ALLOWED_TRIGGERS:
        print(f"Error: invalid trigger {trigger!r}", file=sys.stderr)
        return EXIT_USAGE
    if not funnel_id.strip():
        print("Error: funnel_id is required", file=sys.stderr)
        return EXIT_USAGE

    # Align process env with the requested token so shared-lock resolution
    # and workers see the same production fail-closed rules.
    token = mk04_env(canonical_env(mk04_env_token))
    os.environ["MK04_ENV"] = token

    ctx = prepare_run_context(mk04_env_token, funnel_id=funnel_id.strip(), trigger=trigger)

    with ctx.log_path.open("a", encoding="utf-8") as log:
        _log(log, f"run_pipeline start env={ctx.env_label} run_id={ctx.run_id}")
        _log(log, f"trigger={ctx.trigger} funnel_id={ctx.funnel_id}")
        _log(log, f"log_path={ctx.log_path}")

        try:
            cfg_code, cfg_detail, resolved = validate_config(mk04_env_token)
            _log(log, cfg_detail)
            if cfg_code != EXIT_SUCCESS:
                _record_terminal(
                    ctx,
                    status=STATUS_FAIL,
                    exit_code=cfg_code,
                    failure_reason=cfg_detail,
                    detail=cfg_detail,
                )
                _print_outcome(ctx, STATUS_FAIL)
                print(f"Error: {cfg_detail}", file=sys.stderr)
                return cfg_code

            ctx.resolved_config = resolved
            ctx.config_snapshot_path = write_config_snapshot(ctx.run_dir, resolved)

            ready_code, ready_detail = check_boot_readiness(mk04_env_token)
            _log(log, ready_detail)
            if ready_code != EXIT_SUCCESS:
                _record_terminal(
                    ctx,
                    status=STATUS_FAIL,
                    exit_code=ready_code,
                    failure_reason=ready_detail,
                    detail=ready_detail,
                )
                _print_outcome(ctx, STATUS_FAIL)
                print(f"Error: {ready_detail}", file=sys.stderr)
                return ready_code

            if callable(getattr(resolved, "get", None)):
                job_gate = can_start_new_job(mk04_env_token, resolved)
                _log(log, job_gate.log_message)
                if not job_gate.allowed:
                    data_root = None
                    try:
                        data_root = EnvironmentStatePaths.from_resolved_config(resolved).data_root
                    except Exception:
                        data_root = None
                    block_path = record_disk_pressure_block(
                        environment=ctx.environment,
                        status=job_gate.status,
                        reason=job_gate.reason or "disk pressure blocked new production job",
                        repo_root=REPO_ROOT,
                        data_root=data_root,
                        run_id=ctx.run_id,
                        trigger=ctx.trigger,
                        funnel_id=ctx.funnel_id,
                    )
                    block_detail = job_gate.detail or job_gate.reason or "disk pressure blocked"
                    _log(log, f"disk pressure block recorded: {block_path}")
                    _record_terminal(
                        ctx,
                        status=STATUS_SKIPPED,
                        exit_code=EXIT_SUCCESS,
                        failure_reason=job_gate.reason,
                        detail=block_detail,
                    )
                    _print_outcome(ctx, STATUS_SKIPPED)
                    print(block_detail)
                    return EXIT_SUCCESS

            gate_action, gate_detail = check_scheduled_runtime_gate(mk04_env_token, trigger)
            _log(log, gate_detail)
            if gate_action == "skip":
                _record_terminal(
                    ctx,
                    status=STATUS_SKIPPED,
                    exit_code=EXIT_SUCCESS,
                    failure_reason=gate_detail,
                    detail=gate_detail,
                )
                _print_outcome(ctx, STATUS_SKIPPED)
                print(gate_detail)
                return EXIT_SUCCESS

            lock_ok, lock_detail = acquire_execution_lock(ctx)
            _log(log, lock_detail)
            if not lock_ok:
                _record_terminal(
                    ctx,
                    status=STATUS_SKIPPED,
                    exit_code=EXIT_LOCK_HELD,
                    failure_reason=lock_detail,
                    detail=lock_detail,
                )
                _print_outcome(ctx, STATUS_SKIPPED)
                print(f"Error: {lock_detail}", file=sys.stderr)
                return EXIT_LOCK_HELD

            try:
                ctx.admission = admit_orchestration(
                    environment=ctx.environment,
                    run_id=ctx.run_id,
                    trigger=ctx.trigger,
                )
                _log(log, f"execution gate admitted environment={ctx.environment}")
            except GateError as exc:
                reason = str(exc)
                _log(log, f"gate refused: {reason}")
                release_execution_lock(ctx)
                _record_terminal(
                    ctx,
                    status=STATUS_SKIPPED,
                    exit_code=EXIT_GATE_REFUSED,
                    failure_reason=reason,
                    detail=reason,
                )
                _print_outcome(ctx, STATUS_SKIPPED)
                print(f"Error: {reason}", file=sys.stderr)
                return EXIT_GATE_REFUSED

            # Canonical path: create RUNNING record immediately after lock acquire.
            _start_running_record(ctx)
            _log(log, f"run record created: {ctx.run_dir / 'run_record.json'}")
            if ctx.admission and ctx.admission.production_priority:
                gate = read_gate_status()
                _log(
                    log,
                    f"production priority held; gate_state={gate.state} "
                    f"(waiting for free heavy-resource if needed)",
                )

            try:
                code, detail, pipeline_status, job_ids = invoke_run_funnel(
                    mk04_env_token,
                    ctx.funnel_id,
                    log,
                    run_id=ctx.run_id,
                    environment=ctx.environment,
                )
                ctx.job_ids = list(job_ids)

                # Dev releases turnstile after enqueue so production can wait.
                if ctx.admission and not ctx.admission.production_priority:
                    ctx.admission.release_turnstile()
                    _log(log, "development turnstile released after admission")

                if code != EXIT_SUCCESS:
                    _record_terminal(
                        ctx,
                        status=STATUS_FAIL,
                        exit_code=code,
                        failure_reason=detail,
                        detail=detail,
                        jobs_started=len(job_ids),
                        jobs_completed=0,
                        jobs_failed=1 if job_ids else 0,
                    )
                    status = STATUS_FAIL
                    _log(log, f"run_pipeline finished status={status} exit={code} detail={detail}")
                    _print_outcome(ctx, status)
                    return code

                if pipeline_status == "no_input_available":
                    _record_terminal(
                        ctx,
                        status=STATUS_SUCCESS,
                        exit_code=EXIT_SUCCESS,
                        detail=detail,
                        jobs_started=0,
                        jobs_completed=0,
                        jobs_failed=0,
                    )
                    _log(log, "run_pipeline finished status=SUCCESS (no input)")
                    _print_outcome(ctx, STATUS_SUCCESS)
                    return EXIT_SUCCESS

                # Remain RUNNING while video processing continues.
                wait_code, wait_detail, started, completed, failed = wait_for_video_jobs(
                    mk04_env_token, job_ids, log
                )
                if wait_code == EXIT_SUCCESS:
                    _record_terminal(
                        ctx,
                        status=STATUS_SUCCESS,
                        exit_code=EXIT_SUCCESS,
                        detail=wait_detail,
                        jobs_started=started,
                        jobs_completed=completed,
                        jobs_failed=failed,
                    )
                    status = STATUS_SUCCESS
                else:
                    _record_terminal(
                        ctx,
                        status=STATUS_FAIL,
                        exit_code=wait_code,
                        failure_reason=wait_detail,
                        detail=wait_detail,
                        jobs_started=started,
                        jobs_completed=completed,
                        jobs_failed=failed,
                    )
                    status = STATUS_FAIL
                _log(
                    log,
                    f"run_pipeline finished status={status} exit={wait_code} detail={wait_detail}",
                )
                _print_outcome(ctx, status)
                return wait_code
            except Exception as exc:
                reason = f"unexpected exception: {exc}"
                _log(log, f"FAIL {reason}")
                _record_terminal(
                    ctx,
                    status=STATUS_FAIL,
                    exit_code=EXIT_PIPELINE_FAIL,
                    failure_reason=reason,
                    detail=reason,
                )
                _print_outcome(ctx, STATUS_FAIL)
                print(f"Error: {reason}", file=sys.stderr)
                return EXIT_PIPELINE_FAIL
            finally:
                if ctx.admission is not None:
                    ctx.admission.release()
                    _log(log, "execution gate admission released")
                release_detail = release_execution_lock(ctx)
                _log(log, release_detail)
                ensure_terminal(
                    ctx.run_dir,
                    status=STATUS_FAIL,
                    exit_code=EXIT_PIPELINE_FAIL,
                    failure_reason="run ended without explicit finalisation",
                )
        except Exception as exc:
            # Last-resort: any unexpected failure outside the invoke block.
            reason = f"unexpected exception: {exc}"
            _log(log, f"FAIL {reason}")
            _record_terminal(
                ctx,
                status=STATUS_FAIL,
                exit_code=EXIT_PIPELINE_FAIL,
                failure_reason=reason,
                detail=reason,
            )
            if ctx.lock_acquired:
                _log(log, release_execution_lock(ctx))
            _print_outcome(ctx, STATUS_FAIL)
            print(f"Error: {reason}", file=sys.stderr)
            return EXIT_PIPELINE_FAIL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Shared production pipeline entrypoint (Phases 6–8)",
    )
    parser.add_argument("environment", help="dev or prod")
    parser.add_argument(
        "--funnel-id",
        default="",
        help="Source-input funnel_id for POST /run-funnel (or set RUN_FUNNEL_ID)",
    )
    parser.add_argument(
        "--trigger",
        default="manual_cli",
        help="Trigger source: scheduled|manual_cli|operations_ui|remote_ssh|test",
    )
    args = parser.parse_args(argv)

    funnel_id = (args.funnel_id or os.environ.get("RUN_FUNNEL_ID") or "").strip()
    if not funnel_id:
        print(
            "Error: funnel_id required via --funnel-id or RUN_FUNNEL_ID",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        canonical_env(args.environment)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    return run_pipeline(args.environment, funnel_id=funnel_id, trigger=args.trigger.strip())


if __name__ == "__main__":
    raise SystemExit(main())
