"""Errors the engine's client API raises."""

from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """Base class. ``name`` is a stable machine-readable code."""

    name = "engine_error"

    def __init__(self, message: str, **data: Any) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "message": self.message, **self.data}


class ForbiddenError(EngineError):
    """The actor lacks the right (``can_edit`` or ``can_run``)."""

    name = "forbidden"


class NotFoundError(EngineError):
    name = "not_found"


class KernelUnavailableError(EngineError):
    """No kernel can be started (cap, admission control, interpreter missing)."""

    name = "kernel_unavailable"


class ReadOnlyError(EngineError):
    """The notebook is open read-only (a newer format major)."""

    name = "read_only"


class EngineClosedError(EngineError):
    name = "engine_closed"


class QueryRefusedError(EngineError):
    """An inspection query the engine will not send (``reason`` says why)."""

    name = "query_refused"
