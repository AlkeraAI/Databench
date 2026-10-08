"""What a box posts of its own logs, on its machine credential.

A box with no direct route into the deployment's log store (a RunPod box, a
personal box) posts its supervisor's events to the backend, which re-applies
the allowlist (``alkera_core.compute.box_logs``) and re-emits each one to its
own log. The shape here only bounds the request; what an event may carry is
decided by that allowlist, on the box and again on the server.

In-flight HTTP shapes only: plain ``BaseModel``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from alkera_core.compute.box_logs import MAX_BATCH_EVENTS, TEXT_MAX_CHARS

#: The most fields one event may name; the widest event lists five.
MAX_FIELDS = 16
#: A string field value longer than this is refused outright rather than cut:
#: the box cuts its own text to a fraction of it.
MAX_STRING_CHARS = TEXT_MAX_CHARS * 4


class BoxLogEvent(BaseModel):
    """One event as the box's shipper formats it."""

    timestamp: str | None = Field(default=None, max_length=64)
    level: str = Field(default="info", max_length=16)
    event: str = Field(max_length=96)
    fields: dict[str, Any] = Field(default_factory=dict, title="BoxLogEventFields")

    @field_validator("fields")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_FIELDS:
            raise ValueError(f"at most {MAX_FIELDS} fields")
        for name, item in value.items():
            if len(name) > 64:
                raise ValueError("a field name is too long")
            if item is not None and not isinstance(item, str | int | float | bool):
                raise ValueError("a field value must be a string, a number or a boolean")
            if isinstance(item, str) and len(item) > MAX_STRING_CHARS:
                raise ValueError("a field value is too long")
        return value


class BoxLogBatch(BaseModel):
    """A batch of a box's events."""

    events: list[BoxLogEvent] = Field(max_length=MAX_BATCH_EVENTS)


class BoxLogBatchRead(BaseModel):
    """How many of the batch's events the backend logged, and how many it
    dropped (not allowed, or past the machine's budget)."""

    accepted: int = Field(ge=0)
    dropped: int = Field(ge=0)


__all__ = ["MAX_FIELDS", "MAX_STRING_CHARS", "BoxLogBatch", "BoxLogBatchRead", "BoxLogEvent"]
