"""``sql.execute`` through the real RPC: a service on a Unix socket hosting
the broker, a client connected with the token, the service's run scope
deciding the time window."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from alkera_notebook.rpc import frames
from alkera_notebook.rpc.client import ClientSession, connect
from alkera_notebook.rpc.peer import MethodRegistry
from alkera_notebook.rpc.service import RpcService, UnixEndpoint, new_token
from alkera_notebook.sql.broker import SqlBroker, register_sql_methods
from alkera_notebook.sql.provider import SqlProviderRegistry
from alkera_notebook.sql.providers.fake import FakeConnectionProvider
from nbsqw_support import ALKERA_CODECS_ALL, Kernel, Who, file_bytes, make_db


@dataclass
class Rig:
    service: RpcService
    client: ClientSession
    kernel: Kernel
    provider: FakeConnectionProvider

    def begin(self, run_id: str, actor: Who) -> None:
        self.service.scope.begin(run_id)
        self.kernel.start(run_id, actor)

    def end(self, run_id: str) -> None:
        self.service.scope.end(run_id)
        self.kernel.finish(run_id)

    async def sql(self, sql: str, run_id: str | None, **params: Any) -> Any:
        ctx = {"run_id": run_id, "cell_id": "c1"} if run_id else None
        return await self.client.peer.request(
            "sql.execute", {"sql": sql, "connection": "shop", **params}, ctx=ctx, allow_files=True
        )


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    db = make_db(tmp_path / "shop.duckdb", rows=200_000)
    data = tmp_path / "data"
    data.mkdir()
    kernel = Kernel(data_dir=data, codecs=ALKERA_CODECS_ALL)
    provider = FakeConnectionProvider({"shop": db})
    registry = MethodRegistry()
    register_sql_methods(
        registry, SqlBroker(SqlProviderRegistry([provider]), inline_limit=256 * 1024), kernel
    )
    endpoint = UnixEndpoint.create()
    token = new_token()
    service = RpcService(endpoint, token=token, registry=registry, data_dir=str(data))
    await service.start()
    client = await connect(endpoint.uri, token, codecs=sorted(ALKERA_CODECS_ALL))
    await asyncio.wait_for(service.accept(), 5)
    try:
        yield Rig(service, client, kernel, provider)
    finally:
        await client.close()
        await service.close()
        endpoint.remove()


async def test_a_run_scoped_request_runs_and_returns_an_inline_stream(rig: Rig) -> None:
    rig.begin("r1", Who("person", "alice"))
    result = await rig.sql("select count(*) as n from orders", "r1")
    table = result["table"]
    assert isinstance(table, frames.Segment)
    assert table.codec == "arrow.ipc.stream"
    assert pa.ipc.open_stream(table.data).read_all().to_pydict() == {"n": [200_000]}
    assert result["result_sha256"] == hashlib.sha256(table.data).hexdigest()


async def test_after_the_run_ends_the_service_refuses(rig: Rig) -> None:
    rig.begin("r1", Who("person", "alice"))
    await rig.sql("select 1", "r1")
    rig.end("r1")
    with pytest.raises(frames.RpcError) as err:
        await rig.sql("select 1", "r1")
    assert (err.value.code, err.value.data.get("reason")) == (-32002, "outside_run")
    with pytest.raises(frames.RpcError):
        await rig.sql("select 1", None)
    assert len(rig.provider.executed) == 1


async def test_a_forged_ctx_from_a_background_task_is_attributed_to_the_run(rig: Rig) -> None:
    rig.begin("r9", Who("person", "bob"))

    async def background() -> Any:
        await asyncio.sleep(0)
        return await rig.sql("select 1 as one", "r9")

    await asyncio.gather(background(), background())
    assert rig.provider.executed == [("shop", "bob", "r9"), ("shop", "bob", "r9")]


async def test_a_large_result_arrives_as_a_file_the_client_may_read(rig: Rig) -> None:
    rig.begin("r1", Who("person", "alice"))
    result = await rig.sql("select * from orders", "r1")
    ref = result["table"]
    assert isinstance(ref, frames.FileRef)
    path = Path(ref.path_in(rig.service.data_dir or ""))
    assert hashlib.sha256(file_bytes(path)).hexdigest() == ref.sha256
    with pa.memory_map(str(path)) as source:
        assert pa.ipc.open_file(source).read_all().num_rows == 200_000


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"x": frames.Segment(b"\x00", "arrow.ipc.stream")}, id="segment"),
        pytest.param({"x": b"\x00\x01"}, id="bytes"),
    ],
)
async def test_binary_parameters_are_refused_over_the_wire(
    rig: Rig, params: dict[str, Any]
) -> None:
    rig.begin("r1", Who("person", "alice"))
    with pytest.raises(frames.RpcError) as err:
        await rig.sql("select 1", "r1", params=params)
    assert err.value.code == -32602
    assert rig.provider.executed == []


async def test_sql_errors_keep_their_names_over_the_wire(rig: Rig) -> None:
    rig.begin("r1", Who("person", "alice"))
    with pytest.raises(frames.RpcError) as err:
        await rig.client.peer.request(
            "sql.execute", {"sql": "select 1", "connection": "nope"}, ctx={"run_id": "r1"}
        )
    assert (err.value.code, err.value.name) == (-32010, "sql.unknown_connection")
