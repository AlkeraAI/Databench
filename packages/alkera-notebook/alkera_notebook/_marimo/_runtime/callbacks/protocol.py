# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alkera_notebook._marimo._runtime.request_router import RequestRouter


@runtime_checkable
class KernelCallback(Protocol):
    """A bundle of kernel command handlers that registers itself with a router."""

    def register(self, router: RequestRouter) -> None: ...


@runtime_checkable
class SupportsTeardown(Protocol):
    """A callback that releases resources when the kernel tears down."""

    def teardown(self) -> None: ...


class GlobalsView(Protocol):
    """A view onto user-defined variables (kernel globals)."""

    @property
    def globals(self) -> Mapping[str, Any]: ...
