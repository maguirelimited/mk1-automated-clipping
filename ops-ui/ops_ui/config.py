from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]

_SCRIPTS_CONFIG = BASE_DIR / "scripts" / "config"
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from environment_names import EnvironmentNameError, normalize_runtime_env  # noqa: E402


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _default_port(env: str, *, dev: str, prod: str) -> str:
    return dev if env == "dev" else prod


@dataclass(frozen=True)
class ServiceConfig:
    key: str
    label: str
    base_url: str
    systemd_unit: str
    secret_env: str | None = None
    secret_header: str | None = None


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    control_db_path: Path
    controls_file: Path
    service_timeout_sec: float
    journal_lines: int
    funnel_run_timeout_sec: float
    stuck_running_sec: float
    stuck_queued_sec: float
    stuck_uploading_sec: float
    services: tuple[ServiceConfig, ...]
    ai_service_url: str = "http://127.0.0.1:5075"
    ai_service_unit: str = "mk04-ai-service.service"
    ai_diagnostics_timeout_sec: float = 30.0
    environment: str = "dev"
    upload_mode: str = "dry_run"
    code_root: Path = BASE_DIR
    config_root: Path = Path("/etc/mk04/dev")
    runtime_root: Path = Path("/var/lib/mk04/dev")
    log_root: Path = Path("/var/log/mk04/dev")
    scheduler_mode: str = "manual"
    # Phase 13 security foundation. Tests construct Settings with auth_enabled=False.
    auth_enabled: bool = False
    operator_password: str = ""
    secret_key: str = "mk04-ops-ui-dev-secret-change-me"
    session_lifetime_minutes: int = 60


def load_settings() -> Settings:
    data_dir = Path(_env("OPS_UI_DATA_DIR", str(BASE_DIR / "ops-ui" / "data"))).expanduser()
    try:
        env = normalize_runtime_env(_env("MK04_ENV", "dev"))
    except EnvironmentNameError as exc:
        raise RuntimeError(str(exc)) from exc
    input_port = _env("INPUT_SERVICE_PORT", _default_port(env, dev="5160", prod="5060"))
    video_port = _env("VIDEO_AUTOMATION_PORT", _default_port(env, dev="5150", prod="5050"))
    output_port = _env("OUTPUT_FUNNEL_PORT", _default_port(env, dev="5155", prod="5055"))
    ops_port = _env("OPS_UI_PORT", _default_port(env, dev="5170", prod="5070"))
    ai_port = _env("AI_SERVICE_PORT", _default_port(env, dev="5175", prod="5075"))
    auth_disabled = _env("OPS_UI_AUTH_DISABLED", "0").lower() in {"1", "true", "yes", "on"}
    return Settings(
        environment=env,
        upload_mode=_env("MK04_UPLOAD_MODE", "dry_run"),
        code_root=Path(_env("MK04_ROOT", str(BASE_DIR))).expanduser(),
        config_root=Path(_env("MK04_CONFIG_ROOT", f"/etc/mk04/{env}")).expanduser(),
        runtime_root=Path(_env("MK04_RUNTIME_ROOT", f"/var/lib/mk04/{env}")).expanduser(),
        log_root=Path(_env("MK04_LOG_ROOT", f"/var/log/mk04/{env}")).expanduser(),
        scheduler_mode=_env("MK04_SCHEDULER_MODE", _default_port(env, dev="manual", prod="autonomous")),
        host=_env("OPS_UI_HOST", "127.0.0.1"),
        port=int(ops_port),
        data_dir=data_dir,
        control_db_path=Path(_env("OPS_UI_DB", str(data_dir / "ops_ui.sqlite3"))).expanduser(),
        controls_file=Path(_env("MK04_CONTROLS_FILE", str(data_dir / "controls.json"))).expanduser(),
        service_timeout_sec=float(_env("OPS_UI_SERVICE_TIMEOUT_SEC", "2.5")),
        journal_lines=int(_env("OPS_UI_JOURNAL_LINES", "80")),
        funnel_run_timeout_sec=float(_env("OPS_UI_FUNNEL_RUN_TIMEOUT_SEC", "900")),
        ai_service_url=_env("OPS_AI_SERVICE_URL", _env("AI_SERVICE_URL", f"http://127.0.0.1:{ai_port}")),
        ai_service_unit=_env("OPS_UI_AI_SERVICE_UNIT", "mk04-ai-service.service"),
        ai_diagnostics_timeout_sec=float(_env("OPS_UI_AI_DIAGNOSTICS_TIMEOUT_SEC", "30")),
        stuck_running_sec=float(_env("OPS_UI_STUCK_RUNNING_SEC", "7200")),
        stuck_queued_sec=float(_env("OPS_UI_STUCK_QUEUED_SEC", "1800")),
        stuck_uploading_sec=float(_env("OPS_UI_STUCK_UPLOADING_SEC", "1800")),
        auth_enabled=not auth_disabled,
        operator_password=_env("OPS_UI_OPERATOR_PASSWORD", ""),
        secret_key=_env("OPS_UI_SECRET_KEY", "mk04-ops-ui-dev-secret-change-me"),
        session_lifetime_minutes=int(_env("OPS_UI_SESSION_LIFETIME_MINUTES", "60")),
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url=_env("OPS_SOURCE_INPUT_URL", f"http://127.0.0.1:{input_port}"),
                systemd_unit=_env("OPS_SOURCE_INPUT_UNIT", "mk04-source-input.service"),
                secret_env="INPUT_SERVICE_SECRET",
                secret_header="X-Input-Service-Secret",
            ),
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url=_env("OPS_VIDEO_AUTOMATION_URL", f"http://127.0.0.1:{video_port}"),
                systemd_unit=_env("OPS_VIDEO_AUTOMATION_UNIT", "mk04-video-automation.service"),
                secret_env="VIDEO_AUTOMATION_SECRET",
                secret_header="X-Video-Automation-Secret",
            ),
            ServiceConfig(
                key="output-funnel",
                label="output-funnel",
                base_url=_env("OPS_OUTPUT_FUNNEL_URL", f"http://127.0.0.1:{output_port}"),
                systemd_unit=_env("OPS_OUTPUT_FUNNEL_UNIT", "mk04-output-funnel.service"),
                secret_env="OUTPUT_FUNNEL_SECRET",
                secret_header="X-Output-Funnel-Secret",
            ),
        ),
    )

