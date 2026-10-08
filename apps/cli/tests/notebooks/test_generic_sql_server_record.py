"""A generic SQL connection saved on the server, resolved on a box, queried for real.

The record is the one the web form saves: a SQLAlchemy URL with the password held
apart. The box materializes it into a connection, leases the password through the
workspace's door (a scripted server, the network boundary) and opens the test
Postgres through the generic connector, which vets and pins the host first. A
notebook SQL statement and the agent's ``sql.query`` both run on it, through the
same gated path.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import psycopg
import pyarrow as pa
import pytest
from alkera_cli.cloud_sync import shared_lease
from alkera_cli.cloud_sync.client import (
    ChatConnectionsClient,
    TeamConnectionsClient,
    install_connections_client,
)
from alkera_cli.cloud_sync.lease_scope import LEASE_WORKSPACE
from alkera_cli.notebooks.integrations import AlkeraConnectionsProvider, NotebookToolEnvironment
from alkera_cli.notebooks.integrations.box import alkera_connection_providers
from alkera_cli.plugins.generic_sql.plugin import GenericSqlCapabilities
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools
from alkera_cli.plugins.plugin_base.team_connections import (
    TeamConnectionRecord,
    TeamMemberState,
    to_connection,
)
from alkera_core.config import settings
from alkera_core.project.directory import ProjectDirectory
from alkera_notebook.rpc import frames
from alkera_notebook.sql.broker import RunInfo, SqlBroker
from alkera_notebook.sql.errors import SqlUnavailableError
from alkera_notebook.sql.provider import SqlProviderRegistry, SqlWorkspace
from sqlalchemy.engine import make_url

WORKSPACE = SqlWorkspace(id="ws-pg", root="/workspace", org_id="org-1")
TABLE = "open_generic_sql_orders"
MACHINE_TOKEN = "alkm_" + "y" * 40


@dataclass(frozen=True)
class Who:
    kind: Literal["person", "agent", "system"]
    id: str


@dataclass
class Kernel:
    runs: dict[str, RunInfo]
    kernel_id: str = "k1"
    workspace: SqlWorkspace = WORKSPACE
    codecs: frozenset[str] = frozenset({"arrow.ipc.stream", "rows.json"})
    data_dir: Path | None = None
    sql_row_limit: int | None = None

    def active_run(self, run_id: str) -> RunInfo | None:
        return self.runs.get(run_id)


def _target() -> Any:
    """The test Postgres the suite runs against, as SQLAlchemy reads it."""
    return make_url(os.environ["DATABASE_URL_SYNC"]).set(drivername="postgresql+psycopg")


@pytest.fixture
def warehouse() -> Iterator[None]:
    url = _target()
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"drop table if exists {TABLE}")
        con.execute(
            f"create table {TABLE} as select i as id, i * 3 as amount from generate_series(1, 7) i"
        )
    yield
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"drop table if exists {TABLE}")


@pytest.fixture
def leased(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The workspace's lease door, answering with the database password."""
    asked: list[str] = []
    password = _target().password or ""

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path.endswith("/credential-lease"):
            return httpx.Response(
                200,
                json={
                    "secret": password,
                    "named_secrets": {},
                    "credential_version": 1,
                    "expires_at": "2099-01-01T00:00:00+00:00",
                },
            )
        return httpx.Response(404, json={"detail": "Not found"})

    monkeypatch.setattr(shared_lease, "_cache", {})
    # The test database is on this machine: the operator names it, as a
    # self-hosted install names its own network.
    monkeypatch.setattr(settings, "egress_private_allowlist", _target().host or "localhost")
    monkeypatch.delenv("ALKERA_MACHINE_CREDENTIAL", raising=False)
    box = ChatConnectionsClient(
        api_url="http://api.test",
        token=MACHINE_TOKEN,
        chats=lambda: [],
        workspaces=lambda: [WORKSPACE.id],
        transport=httpx.MockTransport(handle),
    )
    install_connections_client(lambda: box)
    try:
        yield asked
    finally:
        install_connections_client(None)


def _record(*, owner_user_id: str = "") -> TeamConnectionRecord:
    """The row the web form stores: the URL without its password. With
    ``owner_user_id`` it is that person's own ("Just me"), held on the org."""
    url = _target().set(password=None).render_as_string(hide_password=False)
    return TeamConnectionRecord(
        id="rec-pg",
        team_id="org-1" if owner_user_id else "t1",
        team_name="" if owner_user_id else "Data",
        owner_user_id=owner_user_id,
        plugin="generic_sql",
        handle="pg",
        shared_values={"url": url, "environment": "prod"},
        auth_mode="shared",
        auth_method="",
        enabled=True,
        has_shared_secret=True,
        shared_custody="lease",
        credential_version=1,
        updated_at=datetime(2026, 10, 6, tzinfo=UTC),
    )


@pytest.fixture
def operator_login(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """A box run on its operator's login (the self-hosted box, ``cloud-mirror
    run`` signed in as a person), its client installed the way its data plane
    installs it. The person's own door answers the record and leases its
    password; a workspace's door would refuse a person."""
    asked: list[str] = []
    password = _target().password or ""

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path == "/api/v1/me/team-connections":
            return httpx.Response(200, json={"connections": [_record().model_dump(mode="json")]})
        if request.url.path == "/api/v1/me/team-connections/rec-pg/credential-lease":
            return httpx.Response(
                200,
                json={
                    "secret": password,
                    "named_secrets": {},
                    "credential_version": 1,
                    "expires_at": "2099-01-01T00:00:00+00:00",
                },
            )
        return httpx.Response(404, json={"detail": "Not found"})

    monkeypatch.setattr(shared_lease, "_cache", {})
    monkeypatch.setattr(settings, "egress_private_allowlist", _target().host or "localhost")
    monkeypatch.delenv("ALKERA_MACHINE_CREDENTIAL", raising=False)
    person = TeamConnectionsClient(
        api_url="http://api.test", token="person-session", transport=httpx.MockTransport(handle)
    )
    install_connections_client(lambda: person)
    try:
        yield asked
    finally:
        install_connections_client(None)


def _rig(
    tmp_path: Path, record: TeamConnectionRecord | None = None, *, server_scope: bool = False
) -> tuple[ToolRegistry, SqlBroker, ProjectDirectory]:
    project = ProjectDirectory(tmp_path / ".alkera")
    record = record or _record()
    conn = to_connection(
        record, TeamMemberState(added=True, local_handle="pg"), plugins_root=project.plugins_path
    )
    assert conn is not None
    assert "password" not in str(conn.attributes)
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    registry.register_connection(conn, capabilities=GenericSqlCapabilities().capabilities(conn))
    register_sql_tools(registry)

    class Attached:
        async def connection_ids(self, workspace: SqlWorkspace) -> frozenset[str]:
            return frozenset({record.id})

    async def tools() -> ToolRegistry:
        return registry

    async def records() -> list[TeamConnectionRecord]:
        return [record]

    if server_scope:
        # As the box composes it: the workspace's connections read through
        # the client the box installed.
        providers = alkera_connection_providers(project=project, tools=tools, records=records)
        return registry, SqlBroker(SqlProviderRegistry(providers)), project
    provider = AlkeraConnectionsProvider(
        tools=tools,
        records=records,
        scope=Attached(),
        environment=NotebookToolEnvironment(
            decision_sink=DecisionSink(project.path), alkera_dir=project.path
        ),
    )
    return registry, SqlBroker(SqlProviderRegistry([provider])), project


async def _cell(broker: SqlBroker, sql: str, workspace: SqlWorkspace = WORKSPACE) -> Any:
    kernel = Kernel(
        runs={"r1": RunInfo("r1", Who("person", "bob"), "sales.alknb.py")}, workspace=workspace
    )
    return await broker.execute(kernel, {"sql": sql, "connection": "pg"}, {"run_id": "r1"})


async def test_a_notebook_cell_reads_the_server_record_through_the_lease(
    tmp_path: Path, warehouse: None, leased: list[str]
) -> None:
    _registry, broker, _project = _rig(tmp_path)

    result = await _cell(broker, f"select id, amount from {TABLE} where id <= 3 order by id")

    segment = result["table"]
    assert isinstance(segment, frames.Segment)
    table = pa.ipc.open_stream(segment.data).read_all()
    assert table.to_pydict() == {"id": [1, 2, 3], "amount": [3, 6, 9]}
    # Leased for this workspace alone, through the workspace's door.
    assert leased == [f"/api/v1/workspaces/{WORKSPACE.id}/connections/rec-pg/credential-lease"]


async def test_a_box_without_an_org_runs_a_cell_on_the_owners_personal_connection(
    tmp_path: Path, warehouse: None, leased: list[str]
) -> None:
    """A box on its machine credential (``cloud-mirror run``, the localdev box)
    composes every workspace's engine with no org. A cell naming the owner's
    own connection still resolves it and runs, leased for the workspace."""
    _registry, broker, _project = _rig(tmp_path, _record(owner_user_id="owner-1"))
    on_box = SqlWorkspace(id=WORKSPACE.id, root="/workspace")

    result = await _cell(broker, f"select count(*) as n from {TABLE}", workspace=on_box)

    table = pa.ipc.open_stream(result["table"].data).read_all()
    assert table.to_pydict() == {"n": [7]}
    assert leased == [f"/api/v1/workspaces/{WORKSPACE.id}/connections/rec-pg/credential-lease"]


async def test_a_box_on_its_operators_login_runs_a_cell_on_the_workspaces_connection(
    tmp_path: Path, warehouse: None, operator_login: list[str]
) -> None:
    """The self-hosted box serves chats on its operator's login. A notebook
    cell there resolves the workspace's connections through the installed
    client, the person's own set, and leases on the person's door: the same
    client and door the agent's ``sql.query`` uses on that box."""
    _registry, broker, _project = _rig(tmp_path, server_scope=True)
    on_box = SqlWorkspace(id=WORKSPACE.id, root="/workspace")

    result = await _cell(broker, f"select count(*) as n from {TABLE}", workspace=on_box)

    table = pa.ipc.open_stream(result["table"].data).read_all()
    assert table.to_pydict() == {"n": [7]}
    assert operator_login == [
        "/api/v1/me/team-connections",
        "/api/v1/me/team-connections/rec-pg/credential-lease",
    ]


async def test_a_box_whose_login_door_gives_no_answer_refuses_the_cell(
    tmp_path: Path, operator_login: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No answer from the person's door is not "nothing attached": the cell
    is refused as unavailable, never resolved against a guess."""

    async def silent(self: TeamConnectionsClient) -> None:
        return None

    monkeypatch.setattr(TeamConnectionsClient, "fetch_records", silent)
    _registry, broker, _project = _rig(tmp_path, server_scope=True)

    with pytest.raises(SqlUnavailableError, match=f"connections of workspace {WORKSPACE.id} could"):
        await _cell(broker, "select 1")


async def test_a_notebook_cell_runs_any_statement_its_run_was_admitted_for(
    tmp_path: Path, warehouse: None, leased: list[str]
) -> None:
    """A SQL cell runs whatever statement it holds: the run's approval is the
    gate, decided before the kernel is asked, not a statement policy here."""
    _registry, broker, _project = _rig(tmp_path)

    await _cell(broker, f"delete from {TABLE}")

    count = await _cell(broker, f"select count(*) as n from {TABLE}")
    table = pa.ipc.open_stream(count["table"].data).read_all()
    assert table.to_pydict() == {"n": [0]}


async def test_the_agent_sql_tool_reads_the_same_record(
    tmp_path: Path, warehouse: None, leased: list[str]
) -> None:
    registry, _broker, project = _rig(tmp_path)

    # A workspace's agent leases for the workspace it runs in.
    token = LEASE_WORKSPACE.set(WORKSPACE.id)
    try:
        result = await registry.dispatch(
            "sql.query",
            {"mode": "sql", "connection": "pg", "sql": f"select sum(amount) as total from {TABLE}"},
            decision_sink=DecisionSink(project.path),
            alkera_dir=project.path,
        )
    finally:
        LEASE_WORKSPACE.reset(token)

    assert result["preview_rows"] == [[84]]
    assert result["provenance"]["connection_id"] == "rec-pg"


def test_a_box_does_not_build_a_record_whose_host_the_operator_has_not_named(
    tmp_path: Path, leased: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test database is on this machine. Without the operator's opt-in the
    box refuses to build the record at all, so nothing can lease or dial it."""
    monkeypatch.setattr(settings, "egress_private_allowlist", "")
    project = ProjectDirectory(tmp_path / ".alkera")

    conn = to_connection(
        _record(), TeamMemberState(added=True, local_handle="pg"), plugins_root=project.plugins_path
    )

    assert conn is None
    assert leased == []
