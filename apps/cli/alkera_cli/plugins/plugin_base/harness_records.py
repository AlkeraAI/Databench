"""The connection record and query spend, as the harness reads them.

The open harness announces connection rows, records a Test press and settles a
session's warehouse spend through :data:`CONNECTION_RECORDS` and
:data:`SESSION_SPEND`. This module answers both from the connection-state
store, the team connections store and the cost ledger.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING

from alkera_core.connectors.catalog import local_form
from alkera_core.extensions import Extension

from alkera_cli.harness.extension_points import (
    CONNECTION_RECORDS,
    SESSION_SPEND,
    ConnectionIdentity,
    SessionSpend,
)
from alkera_cli.plugins.plugin_base import connection_state
from alkera_cli.plugins.plugin_base.cost import CostLedger
from alkera_cli.plugins.plugin_base.team_connections import (
    TeamConnectionsState,
    TeamConnectionsStore,
    is_configured,
    pending_hints,
    to_connection,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.plugins.plugin_base import Connection, ConnectionFormSchema, PluginRegistry


def _team_state(project: ProjectDirectory) -> TeamConnectionsState:
    return TeamConnectionsStore(project.team_connections_path).load()


class WorkspaceConnectionRecords:
    """Connection rows keyed ``local:<plugin>:<handle>`` or ``team:<record_id>``
    in the connection-state store, with team rows read from the team store."""

    def key_for(self, conn: Connection) -> str:
        return connection_state.key_for(conn)

    def local_key(self, plugin: str, handle: str) -> str:
        return connection_state.local_key(plugin, handle)

    def team_key(self, record_id: str) -> str:
        return connection_state.team_key(record_id)

    def identity_of(self, key: str) -> ConnectionIdentity:
        return ConnectionIdentity(*connection_state.identity_of(key))

    def on_state_written(self, listener: Callable[[Path, str], None]) -> Callable[[], None]:
        return connection_state.on_state_written(listener)

    def records_verification(
        self, project: ProjectDirectory, conn: Connection
    ) -> AbstractAsyncContextManager[None]:
        return connection_state.records_health(project, conn, source="verify")

    def pending_hints(self, project: ProjectDirectory) -> list[str]:
        try:
            state = _team_state(project)
        except Exception:  # a store this machine cannot read offers no hint
            return []
        return pending_hints(state)

    def team_row_identity(
        self, project: ProjectDirectory, record_id: str
    ) -> tuple[str, str] | None:
        state = _team_state(project)
        record = state.records.get(record_id)
        if record is None:
            return None
        member = state.member.get(record_id)
        handle = (member.local_handle if member else "") or record.handle
        return (record.plugin, handle)

    def team_connection(
        self, project: ProjectDirectory, registry: PluginRegistry, plugin: str, handle: str
    ) -> Connection | None:
        match = next(
            (
                (record, member)
                for record, member in registry.team_connection_records()
                if record.plugin == plugin
                and (member.local_handle or record.handle) == handle
                and is_configured(record, member)
            ),
            None,
        )
        if match is None:
            return None
        return to_connection(*match, plugins_root=project.plugins_path)

    def local_form(self, schema: ConnectionFormSchema) -> ConnectionFormSchema:
        return local_form(schema)


class LedgerSessionSpend:
    """A session's spend, read from the per-project cost ledger."""

    def spend(self, project: ProjectDirectory, session_id: str) -> SessionSpend:
        entries = CostLedger(project).read_entries(session_id)
        by_connection: dict[str, float] = {}
        for entry in entries:
            by_connection[entry.connection] = (
                by_connection.get(entry.connection, 0.0) + entry.charged_usd
            )
        return SessionSpend(queries=len(entries), by_connection=by_connection)


RECORDS = WorkspaceConnectionRecords()
SPEND = LedgerSessionSpend()


def _install() -> None:
    CONNECTION_RECORDS.register(RECORDS)
    SESSION_SPEND.register(SPEND)


#: The connection record and query spend, for a composition that installs no
#: data product (the product's harness extension registers the same two).
EXTENSION = Extension(name="alkera.connection-records", install=_install)

__all__ = ["EXTENSION", "RECORDS", "SPEND", "LedgerSessionSpend", "WorkspaceConnectionRecords"]
