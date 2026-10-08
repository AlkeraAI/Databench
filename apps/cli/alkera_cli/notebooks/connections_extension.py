"""The product's notebook SQL: a workspace's Alkera connections on every box.

Installed by the product's composition at every
process entry, so it imports nothing heavy: the providers, and the SQL and
Arrow stack behind them, load the first time a box composes its notebooks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alkera_core.extensions import Extension

from alkera_cli.notebooks.sql_slot import NOTEBOOK_SQL_PROVIDERS, NotebookSqlContext

if TYPE_CHECKING:
    from alkera_notebook.sql.provider import SqlEngineProvider


def connection_providers(context: NotebookSqlContext) -> list[SqlEngineProvider]:
    """The box's workspace-connection providers
    (:func:`alkera_cli.notebooks.integrations.box.box_connection_providers`)."""
    from alkera_cli.notebooks.integrations.box import box_connection_providers

    return box_connection_providers(context)


def _install() -> None:
    NOTEBOOK_SQL_PROVIDERS.register(connection_providers)


#: Puts a workspace's Alkera connections behind every box notebook's SQL.
EXTENSION = Extension(name="notebook_connections", install=_install)


__all__ = ["EXTENSION", "connection_providers"]
