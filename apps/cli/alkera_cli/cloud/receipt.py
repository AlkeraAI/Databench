"""The receipt a promoted result carries to the cloud.

A ``result`` object is credible because of what rides beside its rows: the SQL
that produced them, the connection and role it ran as, the engine, who asked
(the user and the agent acting for them), when, how many rows, how long, and
the parameter values. The daemon assembles it from the tool call it recorded
and posts it with the payload; the cloud pins both on the object. It crosses a
process boundary and is stored, so it is a ``VersionedModel``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from alkera_core.versioning import VersionedModel
from pydantic import Field


class ReceiptPrincipal(VersionedModel):
    """Who the query ran for: the delegating user and the agent (the chat)
    acting inside their session — the same chain the backend audits."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    user_id: str = ""
    agent_id: str = ""
    chain: list[str] = Field(default_factory=list)


class ResultReceipt(VersionedModel):
    """Immutable per promoted result; see the module docstring."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    sql: str = ""
    connection_id: str | None = None
    connection_name: str = ""
    role: str | None = None
    engine: str = ""
    principal: ReceiptPrincipal = Field(default_factory=ReceiptPrincipal)
    executed_at: datetime | None = None
    row_count: int = 0
    duration_ms: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    event_id: str = ""
    """The chat event (the tool result) this receipt was lifted from."""


def receipt_for_tool_result(
    tool_input: Mapping[str, Any],
    output: Mapping[str, Any],
    *,
    event_id: str,
    at: datetime,
    started: datetime | None,
    principal: ReceiptPrincipal | None = None,
    user_id: str = "",
    agent_id: str = "",
) -> ResultReceipt:
    """Lift a receipt off a recorded ``sql.query`` call.

    The tool stamps every executed statement with a ``provenance`` block —
    connection, role, engine, its own clock, the row count and the statement —
    and that is what the receipt copies. A result recorded before the tool
    carried one falls back to what the transcript knows: the call's input, the
    event times, the top-level row count. Every field the cloud requires is
    filled either way — the role and the duration are never ``None``, because
    the cloud's receipt refuses a null where it expects a name or a number.

    Who the query ran for is not in the tool's output: the caller says. A
    ``principal`` it resolved (the member whose turn produced the result, with
    the machine acting for them) is carried whole; a caller that knows only the
    two ids passes them and the chain is built here.
    """
    raw = output.get("provenance")
    provenance: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    duration = _int(provenance.get("duration_ms"))
    if duration is None:
        duration = max(0, int((at - started).total_seconds() * 1000)) if started else 0
    row_count = _int(provenance.get("row_count"))
    if row_count is None:
        row_count = _int(output.get("row_count")) or 0
    return ResultReceipt(
        sql=_text(provenance.get("sql")) or _text(tool_input.get("sql")),
        connection_id=_text(provenance.get("connection_id")) or None,
        connection_name=_text(provenance.get("connection_name"))
        or _text(tool_input.get("connection")),
        role=_text(provenance.get("role")) or _text(output.get("role")),
        engine=_text(provenance.get("engine"))
        or _text(output.get("engine"))
        or _text(tool_input.get("engine")),
        principal=principal
        if principal is not None
        else ReceiptPrincipal(
            user_id=user_id,
            agent_id=agent_id,
            chain=[p for p in (user_id, agent_id) if p],
        ),
        executed_at=_when(provenance.get("executed_at")) or at,
        row_count=row_count,
        duration_ms=duration,
        params=dict(tool_input.get("params") or {}),
        event_id=event_id,
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _when(value: Any) -> datetime | None:
    """A UTC ISO-8601 stamp as an aware datetime; anything else is unknown."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["ReceiptPrincipal", "ResultReceipt", "receipt_for_tool_result"]
