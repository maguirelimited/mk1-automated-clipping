from __future__ import annotations

from .app import create_app
from .config import load_settings


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    app.run(host=settings.host, port=settings.port, debug=False)


if __name__ == "__main__":
    main()

