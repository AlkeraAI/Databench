"""The ``Connector`` ABC: the capability-gated chokepoint.

The ONLY sanctioned path to a data system. A path that reaches a warehouse
without one is outside the boundary. Reads run inside a read-only
transaction; write/destroy/egress are REFUSED unless a valid, unexpired
``cap_token`` minted by the broker for THIS action is presented (the trust
boundary that catches even a dynamically-built tool, ripping
the local engine's ``NodeLineageWriter`` gating).

The cap-token enforcement is LIVE in every concrete connector (duckdb_local,
snowflake): a write/destroy/egress is refused unless ``cap_token.authorizes(...)``
the action the broker approved, and reads run in a read-only path. (Cost is
metered via ``EstimateSQLCostCapability``, not a connector method.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from alkera_cli.contracts.tool_types import (
    CapToken,
    Effect,
    QueryResult,
)
from alkera_cli.plugins.plugin_base.capabilities import IntrospectSchemaCapability
from alkera_cli.plugins.plugin_base.connection import Connection


class Connector(ABC):
    """The capability-gated path to a data system.

    Concrete connectors wrap an unavoidably-sync driver in
    ``asyncio.to_thread`` INSIDE the implementation — never expose sync to
    the framework, whose event loop a blocking driver call would stall.
    """

    connection: Connection

    @abstractmethod
    async def execute(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        limit: int | None,
    ) -> QueryResult:
        """Run ``sql``.

        - reads → inside a READ-ONLY transaction.
        - write/destroy/egress → REFUSE unless ``cap_token`` is valid +
          unexpired for THIS ``ActionDescriptor`` (enforced in every connector).
        """

    @abstractmethod
    async def introspect(self) -> IntrospectSchemaCapability:
        """Return the schema-introspection capability for this connection."""


__all__ = ["Connector"]
