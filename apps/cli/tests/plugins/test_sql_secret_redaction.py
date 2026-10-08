"""A leased secret never reaches a tool result or the transcript.

The connector resolves a connection's credential at the I/O boundary, so a
driver that cannot connect is holding the plaintext when it raises — and plenty
of drivers put the whole DSN in the message. That message became
``ToolError("query failed: …")``, which becomes the tool result the model reads,
the tool card a human sees, and the decision the audit records: the one
LLM-visible sink on this path that was not redacted against the resolved secret.

Nothing observed leaks it today. These pin that it cannot: a connection whose
credential is a sentinel, a driver failure that echoes the whole connection
string, and the sentinel absent from the error, the tool result, and every file
the run wrote under ``.alkera/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.contracts.tool_types import (
    CapToken,
    ColumnMeta,
    QueryResult,
    RelationMeta,
    TableMeta,
)
from alkera_cli.plugins.plugin_base import (
    CapabilitySet,
    Connection,
    Effect,
    Environment,
    IntrospectSchemaCapability,
    RunSQLCapability,
)
from alkera_cli.plugins.plugin_base.permissions import PermissionsConfig
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools
from alkera_cli.plugins.plugin_base.tool import ToolRegistry
from alkera_cli.plugins.plugin_base.urns import warehouse_relation_urn
from alkera_core.connectors.primitives import CredentialRef
from alkera_core.project.directory import ProjectDirectory

SENTINEL = "leased-pw-Zq7T4hVn2LxE"
ENV_VAR = "ALKERA_TEST_LEASED_SECRET"
DSN = f"postgresql://alkera_ro:{SENTINEL}@warehouse.internal:5432/prod"


class _ConnectFailingRunSQL(RunSQLCapability):
    """A driver whose connect fails and echoes the connection string it used —
    the shape psycopg/pymysql/snowflake all take when the host is unreachable."""

    async def run(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        limit: int | None,
        params: dict[str, Any] | None = None,
    ) -> QueryResult:
        raise OSError(f'connection to server failed: could not connect using "{DSN}"')


class _ConnectFailingIntrospect(IntrospectSchemaCapability):
    async def list_relations(self) -> list[RelationMeta]:
        raise OSError(f'could not connect using "{DSN}"')

    async def describe(self, urn: object) -> TableMeta:
        raise OSError(f'could not connect using "{DSN}"')


class _DescribeFailingIntrospect(IntrospectSchemaCapability):
    """Lists fine, then fails on the describe — so the describe sink is proven
    on its own rather than shadowed by the listing."""

    def __init__(self) -> None:
        self.urn = warehouse_relation_urn(
            "postgres", authority="wh", schema="public", table="orders"
        )

    async def list_relations(self) -> list[RelationMeta]:
        return [RelationMeta(urn=self.urn, name="public.orders", kind="table")]

    async def describe(self, urn: object) -> TableMeta:
        raise OSError(f'could not connect using "{DSN}"')


def _registry(
    tmp_path: Path, *, run_cap: object | None = None, introspect: object | None = None
) -> ToolRegistry:
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    conn = Connection(
        handle="wh",
        plugin="postgres",
        dialect="postgres",
        environment=Environment.PROD,
        # The connection persists a POINTER; the secret is resolved at connect.
        credential_ref=CredentialRef(scheme="env", locator=ENV_VAR),
    )
    caps = CapabilitySet()
    if run_cap is not None:
        caps.add(run_cap)
    if introspect is not None:
        caps.add(introspect)
    registry.register_connection(conn, capabilities=caps)
    register_sql_tools(registry)
    return registry


def _written(tmp_path: Path) -> str:
    """Everything the run put on disk under ``.alkera/`` — the transcript, the
    decision log, the blob store."""
    root = tmp_path / ".alkera"
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            parts.append(path.read_text(errors="replace"))
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def _leased(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, SENTINEL)


async def test_a_connect_failure_that_echoes_the_dsn_never_shows_the_secret(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, run_cap=_ConnectFailingRunSQL())
    out = await registry.dispatch(
        "sql.query",
        {"mode": "sql", "connection": "wh", "sql": "select 1", "result_name": "one"},
        session_id="c1",
        permissions=PermissionsConfig(),
    )

    rendered = json.dumps(out)
    assert "error" in out, "the failure still reaches the agent as a tool error"
    assert SENTINEL not in rendered, f"the leased secret reached the tool result: {rendered}"
    assert SENTINEL not in _written(tmp_path), "the leased secret reached a file on disk"
    # It is still a useful error: the agent learns what failed and where.
    assert "query failed" in rendered
    assert "warehouse.internal" in rendered, "the host it could not reach is not a secret"


async def test_a_schema_listing_failure_never_shows_the_secret(tmp_path: Path) -> None:
    registry = _registry(tmp_path, introspect=_ConnectFailingIntrospect())
    out = await registry.dispatch(
        "sql.schema",
        {"mode": "list", "connection": "wh"},
        session_id="c1",
        permissions=PermissionsConfig(),
    )
    rendered = json.dumps(out)
    assert "error" in out and "schema listing failed" in rendered
    assert SENTINEL not in rendered
    assert SENTINEL not in _written(tmp_path)


async def test_a_describe_failure_never_shows_the_secret(tmp_path: Path) -> None:
    registry = _registry(tmp_path, introspect=_DescribeFailingIntrospect())
    out = await registry.dispatch(
        "sql.schema",
        {"mode": "describe", "connection": "wh", "table": "public.orders"},
        session_id="c1",
        permissions=PermissionsConfig(),
    )
    rendered = json.dumps(out)
    assert "error" in out and "describe failed" in rendered
    assert SENTINEL not in rendered
    assert SENTINEL not in _written(tmp_path)


async def test_a_bare_password_in_the_message_is_redacted_too(tmp_path: Path) -> None:
    """Not every driver frames the secret as a DSN — the exact-match redaction
    against the connection's OWN resolved credential is what covers the rest."""

    class _BarePassword(RunSQLCapability):
        async def run(
            self,
            sql: str,
            *,
            effect: Effect,
            cap_token: CapToken | None,
            limit: int | None,
            params: dict[str, Any] | None = None,
        ) -> QueryResult:
            raise OSError(f"authentication failed for user alkera_ro (password {SENTINEL})")

    registry = _registry(tmp_path, run_cap=_BarePassword())
    out = await registry.dispatch(
        "sql.query",
        {"mode": "sql", "connection": "wh", "sql": "select 1", "result_name": "one"},
        session_id="c1",
        permissions=PermissionsConfig(),
    )
    rendered = json.dumps(out)
    assert "error" in out
    assert SENTINEL not in rendered, f"a bare secret reached the tool result: {rendered}"
    assert "authentication failed for user alkera_ro" in rendered


async def test_an_error_with_no_secret_in_it_is_left_alone(tmp_path: Path) -> None:
    """Redaction must not eat a legitimate error: an agent that cannot read the
    driver's complaint cannot fix its query."""

    class _BadSql(RunSQLCapability):
        async def run(
            self,
            sql: str,
            *,
            effect: Effect,
            cap_token: CapToken | None,
            limit: int | None,
            params: dict[str, Any] | None = None,
        ) -> QueryResult:
            raise ValueError('relation "ordrs" does not exist at character 15')

    registry = _registry(tmp_path, run_cap=_BadSql())
    out = await registry.dispatch(
        "sql.query",
        {"mode": "sql", "connection": "wh", "sql": "select * from ordrs", "result_name": "o"},
        session_id="c1",
        permissions=PermissionsConfig(),
    )
    assert out["error"] == 'query failed: relation "ordrs" does not exist at character 15'
    assert SENTINEL not in _written(tmp_path)


async def test_the_columns_of_a_healthy_describe_are_unaffected(tmp_path: Path) -> None:
    """The redaction rides the error path only — a successful describe is
    byte-for-byte what it was."""

    class _HealthyIntrospect(IntrospectSchemaCapability):
        def __init__(self) -> None:
            self.urn = warehouse_relation_urn(
                "postgres", authority="wh", schema="public", table="orders"
            )

        async def list_relations(self) -> list[RelationMeta]:
            return [RelationMeta(urn=self.urn, name="public.orders", kind="table")]

        async def describe(self, urn: object) -> TableMeta:
            return TableMeta(
                urn=self.urn,
                name="public.orders",
                columns=[ColumnMeta(name="id", data_type="int", nullable=False)],
            )

    registry = _registry(tmp_path, introspect=_HealthyIntrospect())
    out = await registry.dispatch(
        "sql.schema",
        {"mode": "describe", "connection": "wh", "table": "orders"},
        session_id="c1",
        permissions=PermissionsConfig(),
    )
    assert "error" not in out, out
    assert out["columns"] == [{"name": "id", "data_type": "int", "nullable": False}]
