"""What a private distribution adds to the daemon.

The open daemon serves the methods under :mod:`alkera_cli.daemon.methods`. A
distribution that serves more registers a :class:`DaemonMethods` into
:data:`DAEMON_METHODS` from its extension's ``install``: ``load`` imports the
modules whose ``@method`` and ``@notification`` declarations register them, and
``errors`` says how the server answers the exceptions those handlers raise. The
daemon reads the point once, when it loads its methods
(:func:`alkera_cli.daemon.methods.load_methods`) and when a server is built, so
every contribution has to be installed during composition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from alkera_core.extensions import ExtensionPoint


@dataclass(frozen=True, slots=True)
class RpcErrorAnswer:
    """How the server answers one exception a contributed handler raises.

    The answer carries the exception's message, which must already be the
    sentence a person reads: an answered error is an expected outcome, so it is
    neither logged as a fault nor sent to Sentry. ``data`` builds the error's
    ``data`` member from the exception, when the client needs more than the
    message to act on it."""

    error: type[Exception]
    code: int
    data: Callable[[Any], Any] | None = None


@dataclass(frozen=True, slots=True)
class DaemonMethods:
    """One distribution's JSON-RPC methods: ``load`` registers them, ``errors``
    answers what they raise.

    ``load`` may run more than once (every server a test suite builds loads the
    methods), so it registers by importing, which Python does once."""

    name: str
    load: Callable[[], None]
    errors: tuple[RpcErrorAnswer, ...] = ()


DAEMON_METHODS: ExtensionPoint[DaemonMethods] = ExtensionPoint("daemon_methods")


__all__ = ["DAEMON_METHODS", "DaemonMethods", "RpcErrorAnswer"]
