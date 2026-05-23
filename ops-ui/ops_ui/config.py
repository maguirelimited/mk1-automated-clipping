from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


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


def load_settings() -> Settings:
    data_dir = Path(_env("OPS_UI_DATA_DIR", str(BASE_DIR / "ops-ui" / "data"))).expanduser()
    return Settings(
        host=_env("OPS_UI_HOST", "127.0.0.1"),
        port=int(_env("OPS_UI_PORT", "5070")),
        data_dir=data_dir,
        control_db_path=Path(_env("OPS_UI_DB", str(data_dir / "ops_ui.sqlite3"))).expanduser(),
        controls_file=Path(_env("MK04_CONTROLS_FILE", str(data_dir / "controls.json"))).expanduser(),
        service_timeout_sec=float(_env("OPS_UI_SERVICE_TIMEOUT_SEC", "2.5")),
        journal_lines=int(_env("OPS_UI_JOURNAL_LINES", "80")),
        funnel_run_timeout_sec=float(_env("OPS_UI_FUNNEL_RUN_TIMEOUT_SEC", "900")),
        stuck_running_sec=float(_env("OPS_UI_STUCK_RUNNING_SEC", "7200")),
        stuck_queued_sec=float(_env("OPS_UI_STUCK_QUEUED_SEC", "1800")),
        stuck_uploading_sec=float(_env("OPS_UI_STUCK_UPLOADING_SEC", "1800")),
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url=_env("OPS_SOURCE_INPUT_URL", "http://127.0.0.1:5060"),
                systemd_unit=_env("OPS_SOURCE_INPUT_UNIT", "mk04-source-input.service"),
                secret_env="INPUT_SERVICE_SECRET",
                secret_header="X-Input-Service-Secret",
            ),
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url=_env("OPS_VIDEO_AUTOMATION_URL", "http://127.0.0.1:5050"),
                systemd_unit=_env("OPS_VIDEO_AUTOMATION_UNIT", "mk04-video-automation.service"),
                secret_env="VIDEO_AUTOMATION_SECRET",
                secret_header="X-Video-Automation-Secret",
            ),
            ServiceConfig(
                key="output-funnel",
                label="output-funnel",
                base_url=_env("OPS_OUTPUT_FUNNEL_URL", "http://127.0.0.1:5055"),
                systemd_unit=_env("OPS_OUTPUT_FUNNEL_UNIT", "mk04-output-funnel.service"),
                secret_env="OUTPUT_FUNNEL_SECRET",
                secret_header="X-Output-Funnel-Secret",
            ),
        ),
    )

