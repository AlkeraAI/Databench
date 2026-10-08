"""What a workspace's listing row says about its chats, read without loading them.

A workspace's row shows how many chats it holds and a binding derived from
them (``alkera_core.objects.workspaces.effective_binding``): one machine only
when every chat is on it, awake when any chat is, a spare only when all are,
writable when any may write, the latest wake. Every one of those is a fact
about the SET of values the chats carry, never about how many carry each, so
the chats are read grouped by those values: one statement for a whole page of
workspaces, and a row per distinct combination rather than per chat. A main
workspace with thousands of chats reads as a handful of rows.

The latest wake is the one fact that is an order rather than a set; it is
compared as an instant in SQL, the way the derivation compares it, and a stamp
that does not parse is no wake.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from alkera_core.schemas.objects import ChatSpec
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The spec keys the binding reads, each grouped on.
_GROUPED: tuple[str, ...] = (
    "machine_id",
    "machine_status",
    "mirror_state",
    "permission_mode",
)

_SUMMARY = text(
    """
    SELECT spec ->> 'workspace_id' AS workspace_id,
           spec ->> 'machine_id' AS machine_id,
           spec ->> 'machine_status' AS machine_status,
           spec ->> 'mirror_state' AS mirror_state,
           spec ->> 'permission_mode' AS permission_mode,
           coalesce(spec ->> 'spare', 'false') = 'true' AS spare,
           count(*) AS chats,
           max(CASE WHEN pg_input_is_valid(spec ->> 'wake_requested_at', 'timestamptz')
                    THEN (spec ->> 'wake_requested_at')::timestamptz END) AS wake,
           min(title) AS title
      FROM workspace_objects
     WHERE type = 'chat' AND deleted_at = 0
       AND (spec ->> 'workspace_id') = ANY(CAST(:keys AS text[]))
     GROUP BY 1, 2, 3, 4, 5, 6
    """
)


@dataclass(slots=True)
class ChatsSummary:
    """A workspace's live chats, as its listing row needs them."""

    count: int = 0
    #: One spec per distinct combination of the values the binding reads; the
    #: latest wake rides on one of them.
    specs: list[ChatSpec] = field(default_factory=list)
    all_spare: bool = True
    #: The title of the one chat, when there is exactly one.
    only_title: str | None = None

    @property
    def empty(self) -> bool:
        return self.count == 0


async def summarize(db: AsyncSession, workspace_ids: Sequence[UUID]) -> dict[UUID, ChatsSummary]:
    """Each workspace's chats summarized, in one grouped read."""
    found: dict[UUID, ChatsSummary] = {
        workspace_id: ChatsSummary() for workspace_id in workspace_ids
    }
    if not workspace_ids:
        return found
    rows = await db.execute(_SUMMARY, {"keys": [str(key) for key in workspace_ids]})
    latest: dict[UUID, tuple[datetime, int]] = {}
    for row in rows.mappings():
        try:
            key = UUID(row["workspace_id"])
        except (TypeError, ValueError):
            continue
        summary = found.setdefault(key, ChatsSummary())
        values: dict[str, Any] = {name: row[name] for name in _GROUPED if row[name] is not None}
        values["spare"] = bool(row["spare"])
        spec = ChatSpec.model_validate(values)
        summary.specs.append(spec)
        summary.count += int(row["chats"])
        summary.all_spare = summary.all_spare and spec.spare
        summary.only_title = row["title"] if summary.count == 1 else None
        wake: datetime | None = row["wake"]
        if wake is not None and (key not in latest or wake > latest[key][0]):
            latest[key] = (wake, len(summary.specs) - 1)
    for key, (wake, at) in latest.items():
        specs = found[key].specs
        specs[at] = specs[at].model_copy(update={"wake_requested_at": wake.isoformat()})
    for summary in found.values():
        if summary.empty:
            summary.all_spare = False
    return found


__all__ = ["ChatsSummary", "summarize"]
