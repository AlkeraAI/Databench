"""Which extensions a backend script installs before it works on the database.

A script that creates rows without serving them (the seeds, the first admin)
has to install the same extensions the running backend does, or an org it
creates lacks the rows an installed domain registers. Which backend that is
belongs to the deployment, not to the script: ``BACKEND_INSTALL`` names the
installer as ``module:function``, the way ``BACKEND_APP`` names the app. Unset,
it is the open platform's own (:func:`backend.open_product.install`); a
distribution's image and Makefile set theirs.
"""

from __future__ import annotations

import importlib
import os

INSTALL_ENV = "BACKEND_INSTALL"
OPEN_INSTALL = "backend.open_product:install"


class CompositionError(RuntimeError):
    """``BACKEND_INSTALL`` does not name an installer this process can call."""


def install_composition(spec: str | None = None) -> str:
    """Run the installer ``spec`` names (default: ``BACKEND_INSTALL``, else the
    open platform's) and return the spec used. Installing is idempotent."""
    chosen = spec or os.environ.get(INSTALL_ENV) or OPEN_INSTALL
    module_name, _, function_name = chosen.partition(":")
    if not module_name or not function_name:
        raise CompositionError(f"{INSTALL_ENV}={chosen!r} is not `module:function`")
    try:
        installer = getattr(importlib.import_module(module_name), function_name)
    except (ImportError, AttributeError) as error:
        raise CompositionError(f"{INSTALL_ENV}={chosen!r} cannot be loaded: {error}") from error
    installer()
    return chosen
