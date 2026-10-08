"""The one backend app the suite drives.

The process's app (``backend.app_factory.process_app``), built from whatever
extensions are installed when the suite starts: none in the open tree, the
product's where a distribution's test layer installed them first (see the root
conftest). A test that reaches the app through the product's root gets this
same instance.
"""

from __future__ import annotations

from backend.app_factory import create_app, process_app
from fastapi import FastAPI

app: FastAPI = process_app()

__all__ = ["app", "create_app"]
