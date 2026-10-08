"""The platform's notebook SQL on a box: a workspace's Alkera connections.

Registered into :data:`~alkera_cli.notebooks.sql_slot.NOTEBOOK_SQL_PROVIDERS`
by :mod:`alkera_cli.notebooks.connections_extension`, which the open platform's
composition installs (the product installs it too). A workspace's connections
are read through the client the box's data plane installed
(:func:`~alkera_cli.cloud_sync.box_sync.box_connections_client`), on a machine
credential or on the operator's login alike.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from alkera_cli.cloud_sync.client import resolve_connections_client
from alkera_cli.notebooks.integrations.connections import (
    AlkeraConnectionsProvider,
    NotebookToolEnvironment,
)
from alkera_cli.notebooks.sql_slot import NotebookSqlContext
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.team_connections import TeamConnectionRecord

if TYPE_CHECKING:
    from alkera_notebook.sql.provider import SqlEngineProvider


class _InstalledWorkspaceScope:
    """A workspace's attached connections as the server answers them, asked
    through the connections client the box installed when it started (it is
    installed after the notebooks are composed, so it is read per query)."""

    async def connection_ids(self, workspace: Any) -> frozenset[str]:
        client = resolve_connections_client()
        if client is None:
            raise RuntimeError("this machine has no connections client yet")
        return await client.records_for_workspace(workspace.id)


def team_records_of(runtime: Any) -> Callable[[], Awaitable[list[TeamConnectionRecord]]]:
    """The team connections synced to the box, as ``runtime``'s plugin
    registry reads them: again on every call, since the sync lane rewrites
    them."""

    async def records() -> list[TeamConnectionRecord]:
        plugins = await runtime.plugin_registry()
        return [record for record, _member in plugins.team_connection_records()]

    return records


def alkera_connection_providers(
    *,
    project: Any,
    tools: Callable[[], Awaitable[Any]],
    records: Callable[[], Awaitable[Iterable[TeamConnectionRecord]]],
    scope: Any = None,
) -> list[SqlEngineProvider]:
    """The platform's SQL provider for every workspace on a box: a name in a
    notebook resolves among the connections attached to its workspace (read
    from the server through the box's connections client), and runs through
    the same audited, metered path ``sql.query`` takes, its credential
    leased for that workspace. ``project`` is the box's project directory
    (its decisions log); ``tools`` the runtime's tool registry; ``records``
    the team connections synced to the box, read again on every statement."""
    return [
        AlkeraConnectionsProvider(
            tools=tools,
            records=records,
            scope=scope or _InstalledWorkspaceScope(),
            environment=NotebookToolEnvironment(
                decision_sink=DecisionSink(project.path), alkera_dir=project.path
            ),
        )
    ]


def box_connection_providers(context: NotebookSqlContext) -> list[SqlEngineProvider]:
    """:func:`alkera_connection_providers` for the box ``context`` names."""
    runtime = context.runtime
    return alkera_connection_providers(
        project=runtime.project, tools=context.tools, records=team_records_of(runtime)
    )


__all__ = [
    "alkera_connection_providers",
    "box_connection_providers",
    "team_records_of",
]
