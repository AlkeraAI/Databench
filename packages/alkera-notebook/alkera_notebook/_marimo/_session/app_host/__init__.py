# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""App isolation for serving multiple notebooks.

Each notebook is hosted in an AppHost, which isolates the notebook from other
running notebooks. Sessions for the same notebook are routed to a single
AppHost.

AppHosts are created and managed by an AppHostPool.
"""

from alkera_notebook._marimo._session.app_host.host import AppHost
from alkera_notebook._marimo._session.app_host.pool import AppHostContext, AppHostPool

__all__ = [
    "AppHost",
    "AppHostContext",
    "AppHostPool",
]
