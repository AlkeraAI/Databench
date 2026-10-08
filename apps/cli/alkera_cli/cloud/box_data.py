"""What a box knows about the data its connections reach: the ``BOX_DATA`` point.

A box re-reads its connections on every poll, keeps each one's schema cards
loaded for the agent, and rewrites the workspace's source cards so a chat
started here is told what it can read. Both belong to the data product. The box
service reads them through this point; a build that registers nothing serves
chats with no schema or source cards, and a connection pass never reports a
change.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from alkera_core.extensions import ExtensionError, ExtensionPoint

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

#: How often a connection's schema cards are re-read when nothing asks sooner.
DEFAULT_SCHEMA_REFRESH_SECONDS = 1800.0


class SchemaCards(Protocol):
    """The schema cards a box keeps for its live connections."""

    async def sync_once(self) -> bool:
        """Re-read the connections and (re)load their cards. True when a
        connection row was added, changed or removed."""
        ...

    async def stop(self) -> None:
        """Stop loading."""
        ...


class BoxData(Protocol):
    """Builds a box's schema cards and rewrites its source cards."""

    def schema_cards(
        self,
        *,
        api_url: str,
        token: str,
        runtime: Any,
        chats: Callable[[], list[str]],
        refresh_interval: float,
        workspaces: Callable[[], list[str]] = list,
        token_source: Callable[[], str] | None = None,
        connections: Any = None,
    ) -> SchemaCards:
        """The schema cards for the chats (``chats``) and workspaces
        (``workspaces``) this box holds."""
        ...

    def refresh_sources(self, project: ProjectDirectory) -> Sequence[str]:
        """Rewrite the workspace's source cards; the connections they name.
        Never raises: a box that cannot describe its sources still serves."""
        ...


#: The box's data plane. At most one is registered.
BOX_DATA: ExtensionPoint[BoxData] = ExtensionPoint("box_data")


class _NoCards:
    async def sync_once(self) -> bool:
        return False

    async def stop(self) -> None:
        return None


class _NoData:
    def schema_cards(self, **_kwargs: Any) -> SchemaCards:
        return _NoCards()

    def refresh_sources(self, project: ProjectDirectory) -> Sequence[str]:
        return ()


def box_data(point: ExtensionPoint[BoxData] = BOX_DATA) -> BoxData:
    """The registered data plane, or one that knows of no data."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError("more than one extension provides the box's data plane")
    return registered[0] if registered else _NoData()


__all__ = [
    "BOX_DATA",
    "DEFAULT_SCHEMA_REFRESH_SECONDS",
    "BoxData",
    "SchemaCards",
    "box_data",
]
