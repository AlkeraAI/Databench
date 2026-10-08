"""Where a box's notebook SQL comes from: an extension point.

The notebook engine resolves a SQL cell's connection name through the
providers it is given. Which providers exist is not the open platform's to
know: the platform's own (a workspace's Alkera connections, run through the
same audited path ``sql.query`` takes) is part of the data product, and a
deployment may add others. So the box's composition reads
:data:`NOTEBOOK_SQL_PROVIDERS`, a set of factories each given the box's
runtime once the box can serve notebooks, and a distribution registers its
factory during composition. With none registered a notebook resolves no
platform connection, which is how the open platform runs on its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from alkera_core.extensions import ExtensionPoint

if TYPE_CHECKING:
    from alkera_notebook.sql.provider import SqlEngineProvider


@dataclass(frozen=True, slots=True)
class NotebookSqlContext:
    """What a provider factory may build on: the box's runtime (its project
    directory, its plugin registry), and its agent tool registry, read on
    demand because it is built after the notebooks are composed."""

    runtime: Any
    tools: Callable[[], Awaitable[Any]]


#: Builds the SQL providers of every workspace's engine on a box.
NotebookSqlFactory = Callable[[NotebookSqlContext], Sequence["SqlEngineProvider"]]

#: The factories a box's notebooks take their SQL providers from, in
#: registration order.
NOTEBOOK_SQL_PROVIDERS: ExtensionPoint[NotebookSqlFactory] = ExtensionPoint(
    "notebook_sql_providers"
)


def sql_providers(context: NotebookSqlContext) -> list[SqlEngineProvider]:
    """Every registered factory's providers for ``context``, in order."""
    return [provider for factory in NOTEBOOK_SQL_PROVIDERS.items() for provider in factory(context)]


__all__ = [
    "NOTEBOOK_SQL_PROVIDERS",
    "NotebookSqlContext",
    "NotebookSqlFactory",
    "sql_providers",
]
