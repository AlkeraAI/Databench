"""What a SQL result says about where it came from — names, never secrets.

``sql.query`` stamps every executed statement with the connection, the
credential role it ran under and the engine, beside the clock and the row
count the connector measured. The role is read off the credential *reference*
(a pointer, never the material): a leased team credential names its role in
the lease locator, a caller's own delegated token is ``per_user``, and a
connection with no credential at all has no role to name.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alkera_core.connectors.primitives import CredentialMode

from alkera_cli.contracts.tool_types import QueryResult
from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.delivery import SqlProvenance

#: The role a locally stored credential (file / keychain / env) stands for: the
#: connection's one primary secret.
PRIMARY_ROLE = "primary"


def credential_role(conn: Connection) -> str:
    """The name of the credential role ``conn`` runs statements under.

    The named role from a ``team_lease`` locator
    (``<record>@<generation>#<role>``), ``per_user`` for a per-user connection
    (the acting user's own token), ``primary`` for any other stored credential,
    and empty when the connection carries no credential reference at all (a
    local DuckDB file, an anonymous endpoint)."""
    if conn.credential_mode == CredentialMode.PER_USER:
        return "per_user"
    ref = conn.credential_ref
    if ref is None:
        return ""
    if ref.scheme == "team_lease":
        _, separator, role = ref.locator.partition("#")
        return role if separator and role else PRIMARY_ROLE
    return PRIMARY_ROLE


def team_connection_id(conn: Connection) -> str:
    """The cloud id of the team connection ``conn`` was materialized from, or
    empty for a connection that exists only on this machine."""
    return str(conn._runtime_bindings.get("team_record_id", "") or "")


def provenance_for(
    conn: Connection,
    result: QueryResult,
    *,
    sql: str,
    started: datetime,
    elapsed_ms: int,
) -> SqlProvenance:
    """The provenance ``sql.query`` attaches to an executed statement.

    The connector's own clock wins when it kept one (``executed_at`` /
    ``duration_ms`` on the :class:`QueryResult`); ``started`` and
    ``elapsed_ms`` — the tool's measurement around the capability call — stand
    in for a capability that does not measure. The engine is the connector's
    when it named one, else the connection's dialect, else its plugin."""
    executed_at = result.executed_at or started
    if executed_at.tzinfo is None:
        executed_at = executed_at.replace(tzinfo=UTC)
    return SqlProvenance(
        connection_id=team_connection_id(conn),
        connection_name=conn.handle,
        role=credential_role(conn),
        engine=result.engine or conn.dialect or conn.plugin,
        executed_at=executed_at.astimezone(UTC).isoformat(timespec="milliseconds"),
        duration_ms=result.duration_ms if result.duration_ms is not None else elapsed_ms,
        row_count=result.row_count,
        sql=sql,
    )


__all__ = ["PRIMARY_ROLE", "credential_role", "provenance_for", "team_connection_id"]
