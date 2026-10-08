"""The runtime host: what ``alkera`` (the public API) talks to inside a
kernel.

The kernel never imports ``alkera``: the person's environment may pin any
version of it, or none. Instead it publishes one object as
``sys.modules["_alkera_runtime"].host`` implementing host protocol 1, and
``alkera._host`` adopts it when ``protocol_version`` is one it speaks.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import _frames as f
from . import display

if TYPE_CHECKING:
    from .runtime import Kernel

PROTOCOL_VERSION = 1
MODULE_NAME = "_alkera_runtime"


class RuntimeHost:
    """Host protocol 1 (``alkera._host.HostV1``)."""

    protocol_version = PROTOCOL_VERSION
    name = "runtime"

    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    # outputs
    def display(self, obj: Any) -> None:
        self._kernel.show(obj)

    def replace(self, obj: Any) -> None:
        self._kernel.show(obj, "replace")

    def clear(self) -> None:
        self._kernel.clear_output()

    def format(self, obj: Any) -> dict[str, Any]:
        return display.format_value(obj)

    # interrupts
    def register_interruptible(self, obj: Any) -> None:
        self._kernel.interrupts.register(obj)

    def unregister_interruptible(self, obj: Any) -> None:
        self._kernel.interrupts.unregister(obj)

    # widgets
    def register_reactive(self, obj: Any) -> None:
        if not any(o is obj for o in self._kernel.reactive):
            self._kernel.reactive.append(obj)

    def open_comm(
        self,
        target_name: str,
        data: dict[str, Any],
        metadata: dict[str, Any],
        on_msg: Callable[[dict[str, Any]], None],
    ) -> Any:
        """Open a Jupyter comm to the frontends; ``on_msg`` receives each
        frontend message on the main thread, as a run. The result has
        ``comm_id``, ``send(data, buffers)`` and ``close()``."""
        return self._kernel.comms.open_comm(target_name, data, metadata, on_msg)  # type: ignore[attr-defined]

    def settings(self) -> dict[str, Any]:
        """The engine's settings from the handshake (``dataframe``,
        ``reactivity``, ``autoreload``, ``args``)."""
        return dict(self._kernel.settings)

    # the service
    def run_context(self) -> dict[str, Any] | None:
        target = self._kernel.current
        if target is None or target.run_id is None:
            run = self._kernel.interrupts.current_run
            return {"run_id": run, "cell_id": None} if run else None
        return {"run_id": target.run_id, "cell_id": target.cell_id}

    def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        """A request to the service, stamped with the active run. Outside a
        run the service refuses run-scoped methods (``forbidden``)."""
        return self._kernel.conn.request(method, params, ctx=self.run_context(), timeout=timeout)

    def args(self) -> dict[str, Any]:
        return dict(self._kernel.args)

    @property
    def data_dir(self) -> str | None:
        return self._kernel.data_dir

    def stop_exception(self) -> type[BaseException]:
        from .runtime import StopCell

        return StopCell

    def codecs(self) -> list[str]:
        from .runtime import _codecs, _lib_versions

        return _codecs(_lib_versions())

    RpcError = f.RpcError


def install(kernel: Kernel) -> RuntimeHost:
    host = RuntimeHost(kernel)
    module = types.ModuleType(MODULE_NAME, "The Alkera kernel's runtime host (protocol 1).")
    module.host = host  # type: ignore[attr-defined]
    module.PROTOCOL_VERSION = PROTOCOL_VERSION  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module
    return host
