"""A stand-in for the kernel's runtime host (protocol 1), published where the
kernel publishes it, so ``alkera.ui`` and ``alkera.sql`` find it the same way.

Its comms record what the element sends; ``call`` can be wired to a real SQL
broker through the RPC's own value encoding."""

from __future__ import annotations

import sys
import types
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Comm:
    comm_id: str
    target_name: str
    data: dict[str, Any]
    metadata: dict[str, Any]
    on_msg: Callable[[dict[str, Any]], None]
    sent: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    def send(self, data: dict[str, Any], buffers: list[bytes] | None = None) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True

    def frontend(self, state: dict[str, Any], msg_id: str = "m1") -> None:
        """A frontend's update, as the kernel delivers it."""
        self.on_msg(
            {
                "header": {"msg_id": msg_id},
                "content": {"comm_id": self.comm_id, "data": {"method": "update", "state": state}},
                "buffers": [],
            }
        )


@dataclass
class FakeHost:
    protocol_version: int = 1
    name: str = "runtime"
    data_dir: str | None = None
    dataframe: str = "pandas"
    comms: list[Comm] = field(default_factory=list)
    displayed: list[Any] = field(default_factory=list)
    reactive: list[Any] = field(default_factory=list)
    interruptible: list[Any] = field(default_factory=list)
    unregistered: list[Any] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    responder: Callable[[str, dict[str, Any]], Any] | None = None

    def open_comm(
        self,
        target_name: str,
        data: dict[str, Any],
        metadata: dict[str, Any],
        on_msg: Callable[[dict[str, Any]], None],
    ) -> Comm:
        comm = Comm(uuid.uuid4().hex, target_name, data, metadata, on_msg)
        self.comms.append(comm)
        return comm

    def register_reactive(self, obj: Any) -> None:
        self.reactive.append(obj)

    def register_interruptible(self, obj: Any) -> None:
        self.interruptible.append(obj)

    def unregister_interruptible(self, obj: Any) -> None:
        self.unregistered.append(obj)

    def display(self, obj: Any) -> None:
        self.displayed.append(obj)

    def settings(self) -> dict[str, Any]:
        return {"dataframe": self.dataframe}

    def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        assert self.responder is not None
        return self.responder(method, params)


@contextmanager
def installed(host: Any) -> Iterator[Any]:
    module = types.ModuleType("_alkera_runtime")
    module.host = host  # type: ignore[attr-defined]
    previous = sys.modules.get("_alkera_runtime")
    sys.modules["_alkera_runtime"] = module
    try:
        yield host
    finally:
        if previous is None:
            sys.modules.pop("_alkera_runtime", None)
        else:
            sys.modules["_alkera_runtime"] = previous
