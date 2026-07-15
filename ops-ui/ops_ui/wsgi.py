"""WSGI entrypoint for ops-ui.

Importing ``ops_ui.app`` must not construct an application or mutate process
environment. Deployments that need a module-level WSGI callable use this
module instead::

    ops_ui.wsgi:app

The primary local/systemd launch path remains ``python -m ops_ui``.
"""

from __future__ import annotations

from .app import create_app
from .config import load_settings

app = create_app(load_settings())
