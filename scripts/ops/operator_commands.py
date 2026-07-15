#!/usr/bin/env python3
"""Thin one-word operator commands: dev, prod, promote.

Bare ``dev`` / ``prod`` start (ensure) the environment service stack.
Pipelines require an explicit ``run`` action. Does not duplicate pipeline,
gate, upload, or promotion logic — only resolves paths/funnels and execs
canonical scripts.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

OPS_DIR = Path(__file__).resolve().parent
REPO_FALLBACK = OPS_DIR.parents[1]
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

from manual_funnel import ManualFunnelError, resolve_manual_funnel  # noqa: E402

MARKER_RUN_PIPELINE = Path("scripts/ops/run-pipeline.sh")
MARKER_PROMOTE = Path("deploy/scripts/promote-to-prod.sh")
MARKER_RUN_SH = Path("run.sh")
MARKER_HEALTH = Path("scripts/ops/health.sh")
MARKER_CONFIG = Path("config")
INSTALL_META_NAME = "mk04-operator-commands.meta"
OWNED_MARKER = "# mk04-operator-command"


class OperatorError(Exception):
    """User-facing operator failure."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _die(exc: OperatorError) -> int:
    print(f"ERROR: {exc.message}", file=sys.stderr)
    return int(exc.code)


def is_valid_dev_root(path: Path) -> bool:
    try:
        root = path.expanduser().resolve()
    except OSError:
        return False
    return (
        (root / MARKER_RUN_PIPELINE).is_file()
        and (root / MARKER_PROMOTE).is_file()
        and (root / MARKER_CONFIG).is_dir()
    )


def discover_dev_root(*, explicit: Path | None = None, meta_path: Path | None = None) -> Path:
    """Resolve development checkout.

    Order: MK04_DEV_ROOT → install meta → this module's repo (when valid).
    """
    if explicit is not None:
        if not is_valid_dev_root(explicit):
            raise OperatorError(
                f"Invalid development root {explicit}: missing run-pipeline.sh, "
                "promote-to-prod.sh, or config/."
            )
        return explicit.expanduser().resolve()

    env_root = (os.environ.get("MK04_DEV_ROOT") or "").strip()
    if env_root:
        path = Path(env_root)
        if not is_valid_dev_root(path):
            raise OperatorError(
                f"MK04_DEV_ROOT={env_root!r} is not a valid development checkout "
                "(need scripts/ops/run-pipeline.sh, deploy/scripts/promote-to-prod.sh, config/)."
            )
        return path.expanduser().resolve()

    meta = meta_path
    if meta is None:
        # Installer places meta next to wrappers; wrappers set MK04_OPERATOR_META.
        raw = (os.environ.get("MK04_OPERATOR_META") or "").strip()
        if raw:
            meta = Path(raw)
    if meta is not None and meta.is_file():
        try:
            data = meta.read_text(encoding="utf-8")
        except OSError as exc:
            raise OperatorError(f"Cannot read install meta {meta}: {exc}") from exc
        for line in data.splitlines():
            if line.startswith("DEV_ROOT="):
                recorded = Path(line.split("=", 1)[1].strip())
                if not is_valid_dev_root(recorded):
                    raise OperatorError(
                        f"Installed DEV_ROOT {recorded} is no longer a valid checkout. "
                        "Re-run deploy/scripts/install-operator-commands.sh."
                    )
                return recorded.expanduser().resolve()

    if is_valid_dev_root(REPO_FALLBACK):
        return REPO_FALLBACK.resolve()

    raise OperatorError(
        "Cannot locate the development checkout. Set MK04_DEV_ROOT or reinstall "
        "operator commands from the checkout."
    )


def prod_base() -> Path:
    return Path(os.environ.get("MK04_PROD_BASE", "/opt/mk04/prod")).expanduser()


def resolve_prod_current() -> Path:
    """Return validated production current release directory."""
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    from promote_release import (  # noqa: PLC0415
        PromoteError,
        prod_layout,
        resolve_release_target,
    )

    layout = prod_layout(prod_base())
    current = layout["current"]
    if not current.exists() and not current.is_symlink():
        raise OperatorError(
            f"Production is not promoted yet ({current} missing). "
            "Complete host bootstrap, then run `promote --no-restart` from the "
            "development checkout, then finish service install.",
            code=1,
        )
    try:
        release = resolve_release_target(current, layout["releases"])
    except PromoteError as exc:
        raise OperatorError(str(exc), code=1) from exc
    if release is None:
        raise OperatorError(
            f"Production current is not a valid release symlink under {layout['releases']}.",
            code=1,
        )
    runner = release / MARKER_RUN_PIPELINE
    if not runner.is_file():
        raise OperatorError(
            f"Production release {release.name} is missing {MARKER_RUN_PIPELINE}.",
            code=1,
        )
    return release


def _upload_mode_display(*, environment: str) -> str:
    if environment == "dev":
        return "development / real posting impossible"
    mode = (os.environ.get("MK04_UPLOAD_MODE") or "dry_run").strip() or "dry_run"
    return mode


def _real_upload_armed() -> bool:
    """Query Prompt 2 authority; never invent a second calculation."""
    root = discover_dev_root()
    of_root = root / "output-funnel"
    text = str(of_root)
    if text not in sys.path:
        sys.path.insert(0, text)
    try:
        from output_funnel.upload_authority import evaluate_real_upload_decision  # noqa: PLC0415
    except Exception:
        # Fail closed for confirmation purposes when authority cannot load.
        return False
    try:
        decision = evaluate_real_upload_decision(environment="prod")
    except Exception:
        return False
    return bool(decision.allow_real_api) and decision.block_reason is None


def _confirm_live(*, confirm_live_flag: bool) -> None:
    if not _real_upload_armed():
        return
    print(
        "WARNING: Production real-upload authority is ARMED. "
        "This run may publish real posts.",
        file=sys.stderr,
    )
    if confirm_live_flag:
        print("Proceeding with --confirm-live.", file=sys.stderr)
        return
    if not sys.stdin.isatty():
        raise OperatorError(
            "Real posting is armed and stdin is not interactive. "
            "Re-run with --confirm-live, or use cron via run-scheduled.sh "
            "(not this wrapper).",
            code=2,
        )
    try:
        answer = input("Type YES to confirm a live production run: ").strip()
    except EOFError as exc:
        raise OperatorError(
            "Real posting confirmation required; no interactive input available. "
            "Use --confirm-live.",
            code=2,
        ) from exc
    if answer != "YES":
        raise OperatorError("Live production run cancelled.", code=2)


def _run_pipeline(
    *,
    script: Path,
    environment: str,
    funnel_id: str,
) -> int:
    cmd = [
        "bash",
        str(script),
        environment,
        "--funnel-id",
        funnel_id,
        "--trigger",
        "manual_cli",
    ]
    proc = subprocess.run(cmd, check=False)
    code = int(proc.returncode)
    if code == 4 and environment == "dev":
        print(
            "Services are not ready (exit 4). Start the development stack with:\n"
            "  dev\n"
            "Do not start duplicate stacks if one is already running.",
            file=sys.stderr,
        )
    if code == 4 and environment == "prod":
        print(
            "Production services are not ready (exit 4). Check:\n"
            "  ./scripts/ops/health.sh prod\n"
            "  ./scripts/ops/status.sh prod\n"
            "Or ensure the stack with: prod",
            file=sys.stderr,
        )
    if code == 6:
        print(
            "Cross-environment gate refused this run (exit 6). "
            "Another environment is active or waiting — see gate diagnostics above.",
            file=sys.stderr,
        )
    return code


def _run_health(script: Path, environment: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["bash", str(script), environment],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return int(proc.returncode), out


def _health_looks_ready(code: int, output: str) -> bool:
    if code != 0:
        return False
    lowered = output.lower()
    if "overall" in lowered and "fail" in lowered:
        # Overall FAIL must not be treated as ready even if exit is odd.
        for line in output.splitlines():
            if line.lower().startswith("overall") and "fail" in line.lower():
                return False
    return True


def start_dev_stack(root: Path) -> int:
    """Ensure the development local stack via ``run.sh --env dev``.

    If health already passes, report and exit without starting a duplicate stack.
    Does not run a pipeline and does not touch uploads/scheduler controls.
    """
    health = root / MARKER_HEALTH
    run_sh = root / MARKER_RUN_SH
    if not run_sh.is_file():
        raise OperatorError(f"Missing development start script: {run_sh}", code=1)

    print("================================================================")
    print("mk04 operator: dev (start stack)")
    print("  Environment:     development")
    print(f"  Code path:       {root}")
    print("  Action:          ensure local service stack")
    print("  Pipeline:        not started (use: dev run [funnel_id])")
    print("================================================================")

    if health.is_file():
        code, output = _run_health(health, "dev")
        if _health_looks_ready(code, output):
            print("Development stack already healthy; not starting a duplicate.")
            if output.strip():
                print(output.rstrip())
            return 0

    print("Starting development stack via ./run.sh --env dev ...")
    print("(Foreground local stack — Ctrl+C stops services.)")
    proc = subprocess.run(
        ["bash", str(run_sh), "--env", "dev"],
        cwd=str(root),
        check=False,
    )
    return int(proc.returncode)


def start_prod_stack(release: Path) -> int:
    """Ensure production systemd units via batched ``systemctl start``.

    Idempotent for already-active units. Does not enable uploads or scheduling.
    Does not run a pipeline.
    """
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))

    try:
        # Always use this checkout's restart helper so bare `prod` gets single-auth
        # batched start even before the repaired release is promoted.
        from restart_service import execute_start  # noqa: PLC0415
    except Exception as exc:
        raise OperatorError(
            f"Cannot load production start helper: {exc}", code=1
        ) from exc

    print("================================================================")
    print("mk04 operator: prod (start stack)")
    print("  Environment:     production")
    print(f"  Current release: {release.name}")
    print(f"  Release path:    {release}")
    print("  Action:          ensure systemd service stack (start, not restart)")
    print("  Uploads/sched:   unchanged (not enabled by this command)")
    print("  Pipeline:        not started (use: prod run [funnel_id])")
    print("================================================================")

    return int(execute_start("prod", "all", dry_run=False, skip_health=False))


def cmd_dev_run(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="dev run",
        description="Manually run one development funnel via the canonical pipeline runner.",
    )
    parser.add_argument("funnel_id", nargs="?", help="Source-input funnel id")
    args = parser.parse_args(list(argv))

    root = discover_dev_root()
    try:
        resolved = resolve_manual_funnel(
            environment="dev",
            explicit_id=args.funnel_id,
            dev_root=root,
        )
    except ManualFunnelError as exc:
        raise OperatorError(str(exc), code=2) from exc

    print("================================================================")
    print("mk04 operator: dev run")
    print("  Environment:     development")
    print(f"  Code path:       {root}")
    print(f"  Selected funnel: {resolved.funnel_id} (via {resolved.source})")
    print(f"  Upload mode:     {_upload_mode_display(environment='dev')}")
    print("================================================================")
    if resolved.warning:
        print(f"WARNING: {resolved.warning}", file=sys.stderr)

    script = root / MARKER_RUN_PIPELINE
    return _run_pipeline(script=script, environment="dev", funnel_id=resolved.funnel_id)


def cmd_prod_run(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="prod run",
        description="Manually run one production funnel via the promoted release.",
    )
    parser.add_argument("funnel_id", nargs="?", help="Source-input funnel id")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Confirm a live-publishing production run when real upload authority is armed",
    )
    args = parser.parse_args(list(argv))

    # Prefer operator implementation from the promoted release when present.
    release = resolve_prod_current()
    release_impl = release / "scripts" / "ops" / "operator_commands.py"
    if (
        release_impl.is_file()
        and release_impl.resolve() != Path(__file__).resolve()
        and not (os.environ.get("MK04_OPERATOR_IN_RELEASE") or "").strip()
    ):
        env = os.environ.copy()
        env["MK04_OPERATOR_IN_RELEASE"] = "1"
        return subprocess.run(
            [sys.executable, str(release_impl), "prod", "run", *argv],
            env=env,
            check=False,
        ).returncode

    try:
        resolved = resolve_manual_funnel(
            environment="prod",
            explicit_id=args.funnel_id,
            # Prefer release tree for catalogue discovery when configs live there;
            # env vars / MK04_CONFIG_ROOT still win inside the resolver.
            dev_root=release if (release / "source-input").is_dir() else discover_dev_root(),
        )
    except ManualFunnelError as exc:
        raise OperatorError(str(exc), code=2) from exc

    print("================================================================")
    print("mk04 operator: prod run")
    print("  Environment:     production")
    print(f"  Current release: {release.name}")
    print(f"  Release path:    {release}")
    print(f"  Selected funnel: {resolved.funnel_id} (via {resolved.source})")
    print(f"  Upload mode:     {_upload_mode_display(environment='prod')}")
    print("================================================================")
    if resolved.warning:
        print(f"WARNING: {resolved.warning}", file=sys.stderr)

    _confirm_live(confirm_live_flag=bool(args.confirm_live))

    script = release / MARKER_RUN_PIPELINE
    return _run_pipeline(script=script, environment="prod", funnel_id=resolved.funnel_id)


def cmd_dev(argv: Sequence[str]) -> int:
    rest = list(argv)
    if rest and rest[0] in {"-h", "--help"}:
        print(
            "Usage:\n"
            "  dev                 Start/ensure the development service stack\n"
            "  dev run [funnel_id] Run one development funnel pipeline\n"
        )
        return 0
    if rest and rest[0] == "run":
        return cmd_dev_run(rest[1:])
    if rest and not rest[0].startswith("-"):
        raise OperatorError(
            f"Unknown dev action {rest[0]!r}. Use `dev` to start the stack, "
            "or `dev run [funnel_id]` to run a pipeline.",
            code=2,
        )
    if rest:
        raise OperatorError(
            f"Unsupported dev option: {rest[0]}. Use `dev` or `dev run [funnel_id]`.",
            code=2,
        )
    return start_dev_stack(discover_dev_root())


def cmd_prod(argv: Sequence[str]) -> int:
    rest = list(argv)
    if rest and rest[0] in {"-h", "--help"}:
        print(
            "Usage:\n"
            "  prod                              Start/ensure the production service stack\n"
            "  prod run [funnel_id] [--confirm-live]\n"
            "                                    Run one production funnel pipeline\n"
        )
        return 0
    if rest and rest[0] == "run":
        return cmd_prod_run(rest[1:])
    if rest and not rest[0].startswith("-"):
        raise OperatorError(
            f"Unknown prod action {rest[0]!r}. Use `prod` to start the stack, "
            "or `prod run [funnel_id]` to run a pipeline.",
            code=2,
        )
    if rest:
        raise OperatorError(
            f"Unsupported prod option: {rest[0]}. Use `prod` or `prod run [funnel_id]`.",
            code=2,
        )

    # Bare start always uses this module (operator wrappers point at the checkout).
    # Do not re-exec into an older release that still treated bare `prod` as a
    # pipeline runner. systemd units still target /opt/mk04/prod/current.
    return start_prod_stack(resolve_prod_current())


PROMOTE_FORWARDABLE = frozenset(
    {
        "--no-restart",
        "--require-clean",
        "--dry-run",
        "--full-tests",
        "--allow-first-bootstrap",
        "--retain-releases",
        "--prod-base",
        "--shared-lock-root",
        "--help",
        "-h",
    }
)


def _prebootstrap_blocks_bare_promote(forwarded: list[str]) -> None:
    if any(
        flag in forwarded
        for flag in ("--no-restart", "--dry-run", "--allow-first-bootstrap", "--help", "-h")
    ):
        return
    layout_current = prod_base() / "current"
    if not layout_current.exists() and not layout_current.is_symlink():
        raise OperatorError(
            "Production current does not exist yet. For first bootstrap staging run:\n"
            "  promote --no-restart\n"
            "Then complete host bootstrap / systemd install before a normal promote.",
            code=1,
        )


def cmd_promote(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="promote",
        description="Atomically promote the development checkout to production.",
        add_help=False,
    )
    # Pass-through: parse only to intercept our errors; forward raw argv.
    forwarded = list(argv)
    # Validate unknown flags that look like options (except values after known opts).
    known_with_value = {"--retain-releases", "--prod-base", "--shared-lock-root", "--source"}
    i = 0
    while i < len(forwarded):
        tok = forwarded[i]
        if tok in {"-h", "--help"}:
            # Let promoter print help.
            break
        if tok.startswith("-") and tok not in PROMOTE_FORWARDABLE and tok not in known_with_value:
            # Allow --source only when we inject it ourselves; reject operator --source override? Spec says forward supported options; --source is set by us.
            if tok == "--source":
                raise OperatorError(
                    "Do not pass --source; promote always uses the recorded development checkout."
                )
            raise OperatorError(
                f"Unsupported promote option: {tok}. "
                "Supported: --no-restart --require-clean --dry-run --full-tests "
                "--allow-first-bootstrap --retain-releases N --prod-base PATH --help"
            )
        if tok in known_with_value:
            i += 2
            continue
        i += 1

    root = discover_dev_root()
    _prebootstrap_blocks_bare_promote(forwarded)

    script = root / MARKER_PROMOTE
    if not script.is_file():
        raise OperatorError(f"Missing promoter: {script}")

    cmd = ["bash", str(script), "--source", str(root), *forwarded]
    return subprocess.run(cmd, check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "Usage:\n"
            "  operator_commands.py dev                 Start/ensure the development stack\n"
            "  operator_commands.py dev run [funnel_id] Run a development funnel pipeline\n"
            "  operator_commands.py prod                Start/ensure the production stack\n"
            "  operator_commands.py prod run [funnel_id] [--confirm-live]\n"
            "                                           Run a production funnel pipeline\n"
            "  operator_commands.py promote [options]   Promote checkout to production\n"
        )
        return 0
    command = argv[0]
    rest = argv[1:]
    try:
        if command == "dev":
            return cmd_dev(rest)
        if command == "prod":
            return cmd_prod(rest)
        if command == "promote":
            return cmd_promote(rest)
        print(f"Unknown command: {command}", file=sys.stderr)
        return 2
    except OperatorError as exc:
        return _die(exc)


if __name__ == "__main__":
    raise SystemExit(main())
