"""Daemon method handlers.

Importing this package registers every open handler via the side-effects of
the `@method` decorator in each submodule; adding a new module means appending
it to the list below. :func:`load_methods` registers those and then every
distribution's contribution to ``DAEMON_METHODS``: it is what the server's
`run_stdio` calls at startup, and what decides the method table a daemon serves.
"""

from __future__ import annotations

from alkera_cli.daemon.extension_points import DAEMON_METHODS, DaemonMethods
from alkera_cli.daemon.methods import (
    auth,
    core,
    environment,
    harness,
    instructions,
    maintenance,
    model_switch,
    plugins,
    preferences,
    project_settings,
    report,
    scheduler,
    working_copy,
)


def load_methods() -> tuple[DaemonMethods, ...]:
    """Register every contributed method after the open ones, in installation
    order, and return the contributions. Loading again is a no-op."""
    contributed = DAEMON_METHODS.items()
    for contribution in contributed:
        contribution.load()
    return contributed


__all__ = [
    "auth",
    "core",
    "environment",
    "harness",
    "instructions",
    "load_methods",
    "maintenance",
    "model_switch",
    "plugins",
    "preferences",
    "project_settings",
    "report",
    "scheduler",
    "working_copy",
]
