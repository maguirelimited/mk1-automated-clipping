from __future__ import annotations

import sys
from pathlib import Path

from .app import create_app
from .config import load_settings


def _configure_http_access_logging() -> None:
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        scripts_dir = candidate / "scripts"
        if (scripts_dir / "http_access_log.py").is_file():
            text = str(scripts_dir)
            if text not in sys.path:
                sys.path.insert(0, text)
            break
    else:
        return
    from http_access_log import OPS_UI_QUIET_PATH_PREFIXES, configure_quiet_http_access_logging

    configure_quiet_http_access_logging(
        service_label="ops-ui",
        quiet_prefixes=OPS_UI_QUIET_PATH_PREFIXES,
    )


def main() -> None:
    _configure_http_access_logging()
    settings = load_settings()
    app = create_app(settings)
    app.run(host=settings.host, port=settings.port, debug=False)


if __name__ == "__main__":
    main()

