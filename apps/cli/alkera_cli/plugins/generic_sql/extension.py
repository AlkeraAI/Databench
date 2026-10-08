"""The open platform's data connector, installed as an extension: the generic SQL
plugin and the SQL agent tools that light up once a connection can run SQL.

The open composition (:mod:`alkera_cli.app.open_product`) installs
:data:`EXTENSION`. The product installs its own richer plugin under the same name
and registers the SQL tools with its data tools, so it does not install this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alkera_core.extensions import Extension

from alkera_cli.plugins.generic_sql.plugin import GenericSqlPlugin
from alkera_cli.plugins.plugin_base.agent_tools import AGENT_TOOLS, ToolBuild, ToolServices
from alkera_cli.plugins.plugin_base.capabilities import RunSQLCapability
from alkera_cli.plugins.plugin_base.plugin import CLI_PLUGINS
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.tool import ToolRegistry


class SqlToolSource:
    """``sql.query``, ``sql.schema`` and ``sql.connections``, once any connection
    the build registered can run SQL. Provides no store: with no cost ledger the
    cost gate stands aside."""

    name = "alkera.sql-tools"

    def services(self, build: ToolBuild) -> ToolServices:
        return ToolServices()

    def register(self, registry: ToolRegistry, build: ToolBuild) -> None:
        present = [caps for caps in build.capabilities if caps is not None]
        if build.every_family or any(caps.has(RunSQLCapability) for caps in present):
            register_sql_tools(registry)


SQL_TOOLS = SqlToolSource()


def _install() -> None:
    CLI_PLUGINS.register(GenericSqlPlugin)
    AGENT_TOOLS.register(SQL_TOOLS)


EXTENSION = Extension(name="alkera.generic-sql", install=_install)

__all__ = ["EXTENSION", "SQL_TOOLS", "SqlToolSource"]
