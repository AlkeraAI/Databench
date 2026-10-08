"""Typed Python client for the Alkera API.

Most of the package is generated from ``packages/shared-openapi/openapi.json``
into ``alkera_sdk._generated`` (see the package README for regeneration).
The hand-written ``AlkeraClient`` wraps that generated surface in a
namespaced, cookie-aware client suitable for scripts, the CLI, and
integration tests.

Generated models can be imported from ``alkera_sdk.models`` (re-exported
verbatim from ``alkera_sdk._generated.models``).
"""

from __future__ import annotations

from alkera_sdk import _generated as _generated
from alkera_sdk._generated import models as models
from alkera_sdk._generated.errors import UnexpectedStatus
from alkera_sdk.client import AlkeraClient

__version__: str = "0.0.0"

__all__ = ["AlkeraClient", "UnexpectedStatus", "__version__", "models"]
