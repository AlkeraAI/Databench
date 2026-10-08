"""The engine's ``sql.execute``: run scoping, workspace scoping, statement
policy, cancellation and result encoding, against real DuckDB files."""

from __future__ import annotations

import asyncio
import decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from alkera_notebook.rpc import frames
from alkera_notebook.sql.broker import ByteBudget, SqlBroker
from alkera_notebook.sql.errors import (
    InvalidParamsError,
    OutsideRunError,
    QueryCancelledError,
    QueryFailedError,
    ResultTooLargeError,
    SqlError,
    SqlUnavailableError,
    StatementRefusedError,
    UnknownConnectionError,
)
from alkera_notebook.sql.policy import PolicyDecision, StatementKind, keyword_classifier
from alkera_notebook.sql.provider import SqlProviderRegistry, SqlWorkspace
from alkera_notebook.sql.providers.fake import FakeConnection, FakeConnectionProvider
from nbsqw_support import AGENT, SYSTEM, Kernel, Who, file_bytes, make_db


class Answers:
    """A person's answers to confirmation prompts, in order."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, str, StatementKind]] = []

    async def confirm(self, run_id: str, actor: Any, statement: str, kind: StatementKind) -> bool:
        self.asked.append((run_id, actor.id, kind))
        return self.answers.pop(0) if self.answers else False


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return make_db(tmp_path / "shop.duckdb")


@pytest.fixture
def kernel(tmp_path: Path) -> Kernel:
    data = tmp_path / "data"
    data.mkdir()
    return Kernel(data_dir=data)


class ConfirmWrites:
    """A deployment's own rule on top of the default: writes need a yes."""

    def check(self, statement: str, actor: Any) -> PolicyDecision:
        kind = keyword_classifier(statement)
        if kind == "read":
            return PolicyDecision("allow", kind)
        return PolicyDecision("confirm", kind, "this deployment asks before writes")


def broker_for(provider: FakeConnectionProvider, **kw: Any) -> SqlBroker:
    return SqlBroker(SqlProviderRegistry([provider]), **kw)


def read_stream(segment: bytes) -> pa.Table:
    return pa.ipc.open_stream(segment).read_all()


async def run_sql(
    broker: SqlBroker,
    kernel: Kernel,
    sql: str,
    *,
    run_id: str = "r1",
    connection: str = "shop",
    **params: Any,
) -> Any:
    return await broker.execute(
        kernel,
        {"sql": sql, "connection": connection, **params},
        {"run_id": run_id, "cell_id": "c1"},
    )


# ------------------------------------------------------------------ run scoping


async def test_refused_outside_any_run(db: Path, kernel: Kernel) -> None:
    provider = FakeConnectionProvider({"shop": db})
    with pytest.raises(OutsideRunError) as err:
        await run_sql(broker_for(provider), kernel, "select 1")
    assert err.value.to_error() == {
        "code": -32002,
        "message": "sql.execute is accepted only while a run is executing",
        "data": {"name": "forbidden", "reason": "outside_run"},
    }
    assert provider.executed == []


async def test_refused_after_the_run_ends(db: Path, kernel: Kernel) -> None:
    provider = FakeConnectionProvider({"shop": db})
    broker = broker_for(provider)
    kernel.start("r1")
    await run_sql(broker, kernel, "select 1")
    kernel.finish("r1")
    with pytest.raises(OutsideRunError):
        await run_sql(broker, kernel, "select 1")
    assert len(provider.executed) == 1


async def test_any_caller_during_a_run_is_accepted_and_attributed_to_it(
    db: Path, kernel: Kernel
) -> None:
    """Run scoping is a time window: a request naming the active run, from any
    thread in the kernel, runs as that run's requester."""
    provider = FakeConnectionProvider({"shop": db})
    broker = broker_for(provider)
    kernel.start("r7", Who("person", "bob"))

    async def from_background() -> Any:
        return await run_sql(broker, kernel, "select count(*) as n from orders", run_id="r7")

    response = await asyncio.create_task(from_background())
    assert read_stream(response["table"].data).to_pydict() == {"n": [100]}
    assert provider.executed == [("shop", "bob", "r7")]


async def test_missing_run_id_is_outside_run(db: Path, kernel: Kernel) -> None:
    broker = broker_for(FakeConnectionProvider({"shop": db}))
    kernel.start("r1")
    with pytest.raises(OutsideRunError):
        await broker.execute(kernel, {"sql": "select 1", "connection": "shop"}, {})


# ------------------------------------------------------------------ connections


async def test_unknown_connection(db: Path, kernel: Kernel) -> None:
    kernel.start("r1")
    with pytest.raises(UnknownConnectionError) as err:
        await run_sql(
            broker_for(FakeConnectionProvider({"shop": db})), kernel, "select 1", connection="nope"
        )
    assert err.value.to_error()["code"] == -32010
    assert err.value.to_error()["data"] == {"name": "sql.unknown_connection", "connection": "nope"}


async def test_first_registered_provider_wins(db: Path, tmp_path: Path, kernel: Kernel) -> None:
    other = make_db(tmp_path / "other.duckdb", rows=3)
    first = FakeConnectionProvider({"shop": other}, name="platform")
    second = FakeConnectionProvider({"shop": db}, name="core")
    registry = SqlProviderRegistry([second])
    registry.register(first, first=True)
    kernel.start("r1")
    response = await SqlBroker(registry).execute(
        kernel, {"sql": "select count(*) n from orders", "connection": "shop"}, {"run_id": "r1"}
    )
    assert read_stream(response["table"].data).to_pydict() == {"n": [3]}
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeConnectionProvider({}, name="core"))


# ------------------------------------------------------------------ statement policy


@pytest.mark.parametrize("who", [None, AGENT, SYSTEM], ids=["person", "agent", "system"])
async def test_a_run_writes_with_no_statement_rule_by_default(
    db: Path, kernel: Kernel, who: Any
) -> None:
    """An INSERT in a run that was allowed to start runs: the run's approval is
    the gate, and nothing asks again per statement."""
    provider = FakeConnectionProvider({"shop": FakeConnection(db, read_only=False)})
    broker = broker_for(provider)
    if who is None:
        kernel.start("r1")
    else:
        kernel.start("r1", who)
    await run_sql(broker, kernel, "insert into orders select * from orders limit 1")
    await run_sql(broker, kernel, "create table t2 as select 1 as one")
    response = await run_sql(broker, kernel, "select count(*) n from orders")
    assert read_stream(response["table"].data).to_pydict() == {"n": [101]}


async def test_a_person_confirms_once_per_run(db: Path, kernel: Kernel) -> None:
    provider = FakeConnectionProvider({"shop": FakeConnection(db, read_only=False)})
    answers = Answers(True, True)
    broker = broker_for(provider, policy=ConfirmWrites(), confirmer=answers)
    kernel.start("r1")
    await run_sql(broker, kernel, "delete from orders where id < 10")
    await run_sql(broker, kernel, "delete from orders where id < 20")
    assert answers.asked == [("r1", "alice", "write")]
    # The next run asks again.
    kernel.finish("r1")
    broker.run_finished(kernel.kernel_id, "r1")
    kernel.start("r2")
    await run_sql(broker, kernel, "create table t2 as select 1", run_id="r2")
    assert answers.asked == [("r1", "alice", "write"), ("r2", "alice", "ddl")]
    response = await run_sql(broker, kernel, "select count(*) n from orders", run_id="r2")
    assert read_stream(response["table"].data).to_pydict() == {"n": [80]}


async def test_a_declined_write_does_not_run(db: Path, kernel: Kernel) -> None:
    provider = FakeConnectionProvider({"shop": FakeConnection(db, read_only=False)})
    broker = broker_for(provider, policy=ConfirmWrites(), confirmer=Answers(False))
    kernel.start("r1")
    with pytest.raises(StatementRefusedError):
        await run_sql(broker, kernel, "delete from orders")
    assert provider.executed == []
    response = await run_sql(broker, kernel, "select count(*) n from orders")
    assert read_stream(response["table"].data).to_pydict() == {"n": [100]}


# ------------------------------------------------------------------ cancellation


async def test_interrupt_cancels_a_waiting_statement_at_the_provider(
    db: Path, kernel: Kernel
) -> None:
    provider = FakeConnectionProvider({"shop": db}, delay_s=30)
    broker = broker_for(provider)
    kernel.start("r1")
    task = asyncio.create_task(run_sql(broker, kernel, "select 1"))
    await asyncio.sleep(0.1)
    assert await broker.cancel_run(kernel.kernel_id, "r1") == 1
    with pytest.raises(QueryCancelledError) as err:
        await task
    assert err.value.to_error()["code"] == -32004
    assert len(provider.cancelled) == 1
    assert provider.executed == []


async def test_interrupt_stops_a_long_duckdb_query(db: Path, kernel: Kernel) -> None:
    provider = FakeConnectionProvider({"shop": db})
    broker = broker_for(provider)
    kernel.start("r1")
    slow = "select count(*) from range(100000000) a, range(100000) b where a.range * b.range = -1"
    task = asyncio.create_task(run_sql(broker, kernel, slow))
    await asyncio.sleep(0.5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await broker.cancel_run(kernel.kernel_id, "r1")
    with pytest.raises(QueryCancelledError):
        await task
    assert loop.time() - started < 5
    assert provider.cancelled


async def test_a_request_cancelled_by_its_caller_propagates(db: Path, kernel: Kernel) -> None:
    provider = FakeConnectionProvider({"shop": db}, delay_s=30)
    broker = broker_for(provider)
    kernel.start("r1")
    task = asyncio.create_task(run_sql(broker, kernel, "select 1"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert provider.cancelled


async def test_cancel_run_with_nothing_in_flight(kernel: Kernel) -> None:
    broker = SqlBroker(SqlProviderRegistry())
    assert await broker.cancel_run("k1", "r9") == 0


# ------------------------------------------------------------------ encoding


async def test_inline_stream_result(db: Path, kernel: Kernel) -> None:
    kernel.start("r1")
    response = await run_sql(
        broker_for(FakeConnectionProvider({"shop": db})), kernel, "select * from orders order by id"
    )
    result = response
    segment = response["table"].data
    assert isinstance(result["table"], frames.Segment)
    assert (result["table"].codec, result["table"].meta) == ("arrow.ipc.stream", {"rows": 100})
    assert result["rows"] == 100
    assert result["bytes"] == len(segment)
    assert result["truncated"] is False
    assert result["result_sha256"] == hashlib.sha256(segment).hexdigest()
    table = read_stream(segment)
    assert table.column("id").to_pylist()[:3] == [0, 1, 2]
    first = pa.ipc.read_message(pa.BufferReader(segment))
    assert first.metadata_version == pa.ipc.MetadataVersion.V5
    assert list(kernel.data_dir.iterdir()) == []  # type: ignore[union-attr]


async def test_large_result_goes_to_a_file_in_the_data_dir(tmp_path: Path, kernel: Kernel) -> None:
    db = make_db(tmp_path / "big.duckdb", rows=200_000)
    broker = broker_for(FakeConnectionProvider({"shop": db}), inline_limit=64 * 1024)
    kernel.start("r1")
    response = await run_sql(broker, kernel, "select * from orders order by id")
    table_ref = response["table"]
    assert isinstance(table_ref, frames.FileRef)
    assert table_ref.codec == "arrow.ipc.file"
    path = Path(table_ref.path_in(str(kernel.data_dir)))
    data = file_bytes(path)
    assert table_ref.bytes == len(data) == response["bytes"]
    assert table_ref.sha256 == hashlib.sha256(data).hexdigest() == response["result_sha256"]
    with pa.memory_map(str(path)) as source:
        table = pa.ipc.open_file(source).read_all()
    assert table.num_rows == 200_000
    assert table.column("id").to_pylist()[-1] == 199_999
    assert [p.name for p in kernel.data_dir.iterdir()] == [table_ref.name]  # type: ignore[union-attr]


async def test_rows_json_for_a_kernel_without_arrow(db: Path, tmp_path: Path) -> None:
    kernel = Kernel(data_dir=None, codecs=frozenset({"json", "rows.json"}))
    kernel.start("r1")
    sql = (
        "select 1 as i, 1.5::decimal(10,2) as d, timestamp '2026-10-05 12:00:00' as ts, "
        "date '2026-10-05' as day, "
        "'x' as s, null as n, 'inf'::double as f, 9007199254740993::bigint as big, {'$t': 1} as obj"
    )
    response = await run_sql(broker_for(FakeConnectionProvider({"shop": db})), kernel, sql)
    assert response["table"].codec == "rows.json"
    body = json.loads(response["table"].data)
    assert [c["name"] for c in body["columns"]] == [
        "i",
        "d",
        "ts",
        "day",
        "s",
        "n",
        "f",
        "big",
        "obj",
    ]
    assert body["rows"] == [
        [
            1,
            {"$t": "decimal", "v": "1.50"},
            {"$t": "datetime", "v": "2026-10-05T12:00:00"},
            {"$t": "date", "v": "2026-10-05"},
            "x",
            None,
            {"$t": "float", "v": "inf"},
            {"$t": "int", "v": "9007199254740993"},
            {"$t": "object", "v": {"$t": 1}},
        ]
    ]
    assert response["result_sha256"] == hashlib.sha256(response["table"].data).hexdigest()


async def test_a_kernel_with_no_table_codec_is_told(db: Path) -> None:
    kernel = Kernel(data_dir=None, codecs=frozenset({"json"}))
    kernel.start("r1")
    with pytest.raises(InvalidParamsError, match="no table codec"):
        await run_sql(broker_for(FakeConnectionProvider({"shop": db})), kernel, "select 1")


async def test_a_large_result_has_no_cap(tmp_path: Path, kernel: Kernel) -> None:
    """The whole result becomes a frame in the kernel: past the inline limit
    it travels as a file, whatever its size."""
    db = make_db(tmp_path / "big.duckdb", rows=200_000)
    broker = broker_for(FakeConnectionProvider({"shop": db}), inline_limit=16 * 1024)
    kernel.start("r1")
    response = await run_sql(broker, kernel, "select * from orders")
    assert response["rows"] == 200_000
    assert response["truncated"] is False
    assert response["bytes"] > 64 * 16 * 1024
    assert broker.budget.available == broker.budget.capacity


@pytest.mark.parametrize(
    ("sql", "limit", "rows", "cut"),
    [
        pytest.param("select * from orders", 30, 30, True, id="cut"),
        pytest.param("select * from orders", 100, 100, False, id="exactly-at-the-limit"),
        pytest.param("select * from orders", 500, 100, False, id="under"),
        pytest.param("select * from orders limit 60", 30, 60, False, id="own-limit-wins"),
        pytest.param(
            "select * from (select * from orders limit 90) t",
            30,
            30,
            True,
            id="inner-limit-is-not-own",
        ),
        pytest.param("select * from orders", None, 100, False, id="no-limit-set"),
    ],
)
async def test_sql_row_limit(
    db: Path, kernel: Kernel, sql: str, limit: int | None, rows: int, cut: bool
) -> None:
    kernel.sql_row_limit = limit
    kernel.start("r1")
    broker = broker_for(FakeConnectionProvider({"shop": db}))
    response = await run_sql(broker, kernel, sql)
    table = read_stream(response["table"].data)
    assert table.num_rows == rows == response["rows"]
    assert response["truncated"] is cut
    if cut:
        assert response["row_limit"] == limit
        assert response["notice"].startswith(f"Showing the first {limit} rows")
    else:
        assert "row_limit" not in response


async def test_sql_row_limit_cuts_a_file_result_and_rows_json(tmp_path: Path) -> None:
    db = make_db(tmp_path / "big.duckdb", rows=200_000)
    data = tmp_path / "data"
    data.mkdir()
    kernel = Kernel(data_dir=data, sql_row_limit=150_000)
    kernel.start("r1")
    response = await run_sql(
        broker_for(FakeConnectionProvider({"shop": db}), inline_limit=16 * 1024),
        kernel,
        "select * from orders",
    )
    assert response["table"].codec == "arrow.ipc.file"
    assert response["rows"] == 150_000
    assert response["truncated"] is True
    rows_kernel = Kernel(data_dir=None, codecs=frozenset({"rows.json"}), sql_row_limit=5)
    rows_kernel.start("r1")
    response = await run_sql(
        broker_for(FakeConnectionProvider({"shop": db})),
        rows_kernel,
        "select id from orders order by id",
    )
    assert json.loads(response["table"].data)["rows"] == [[0], [1], [2], [3], [4]]
    assert response["truncated"] is True


async def test_rows_json_over_the_inline_limit_is_too_large(tmp_path: Path) -> None:
    db = make_db(tmp_path / "big.duckdb", rows=50_000)
    kernel = Kernel(data_dir=None, codecs=frozenset({"rows.json"}))
    kernel.start("r1")
    with pytest.raises(ResultTooLargeError) as err:
        await run_sql(
            broker_for(FakeConnectionProvider({"shop": db}), inline_limit=64 * 1024),
            kernel,
            "select * from orders",
        )
    assert "polars or pyarrow" in err.value.to_error()["data"]["hint"]


async def test_large_result_without_a_file_codec_is_too_large(tmp_path: Path) -> None:
    db = make_db(tmp_path / "big.duckdb", rows=200_000)
    kernel = Kernel(data_dir=tmp_path, codecs=frozenset({"arrow.ipc.stream"}))
    kernel.start("r1")
    with pytest.raises(ResultTooLargeError):
        await run_sql(
            broker_for(FakeConnectionProvider({"shop": db}), inline_limit=64 * 1024),
            kernel,
            "select * from orders",
        )


# ------------------------------------------------------------------ parameters


async def test_bound_parameters_with_tagged_scalars(db: Path, kernel: Kernel) -> None:
    kernel.start("r1")
    response = await run_sql(
        broker_for(FakeConnectionProvider({"shop": db})),
        kernel,
        "select count(*) n from orders where amount > $amount and customer = $who",
        params={"amount": decimal.Decimal("100.5"), "who": "c3"},
    )
    expected = sum(1 for i in range(100) if i * 1.5 > 100.5 and i % 7 == 3)
    assert expected > 0
    assert read_stream(response["table"].data).to_pydict() == {"n": [expected]}


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"x": frames.Segment(b"ARROW1", "arrow.ipc.stream")}, id="arrow-segment"),
        pytest.param(
            {"x": frames.FileRef("evil.arrow", "arrow.ipc.file", 1, "0" * 64)}, id="arrow-file"
        ),
        pytest.param([b"\x00\x01"], id="bytes"),
        pytest.param({"nested": [{"x": frames.Segment(b"\x00")}]}, id="nested-segment"),
        pytest.param("select", id="not-a-container"),
    ],
)
async def test_binary_parameters_are_refused_undecoded(
    db: Path, kernel: Kernel, params: Any
) -> None:
    """The engine never decodes Arrow (or anything binary) a kernel produced:
    a parameter carrying a stream (even one with arrow.py_extension_type) is
    refused before anything reads it."""
    provider = FakeConnectionProvider({"shop": db})
    kernel.start("r1")
    with pytest.raises(InvalidParamsError):
        await run_sql(broker_for(provider), kernel, "select 1", params=params)
    assert provider.executed == []


@pytest.mark.parametrize(
    ("params", "message"),
    [
        pytest.param({"connection": "shop"}, "sql must be", id="no-sql"),
        pytest.param({"sql": "   ", "connection": "shop"}, "sql must be", id="blank-sql"),
        pytest.param({"sql": "select 1"}, "connection must", id="no-connection"),
    ],
)
async def test_malformed_requests(
    db: Path, kernel: Kernel, params: dict[str, Any], message: str
) -> None:
    kernel.start("r1")
    with pytest.raises(InvalidParamsError, match=message):
        await broker_for(FakeConnectionProvider({"shop": db})).execute(
            kernel, params, {"run_id": "r1"}
        )


# ------------------------------------------------------------------ engine DuckDB


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("select * from read_text('/etc/hosts')", id="read-text"),
        pytest.param("select * from read_csv('/etc/hosts')", id="read-csv"),
        pytest.param("copy (select 1) to '{out}'", id="copy-to"),
        pytest.param("install httpfs", id="install"),
        pytest.param("load httpfs", id="load"),
        pytest.param("attach '{other}' as o", id="attach"),
        pytest.param("set enable_external_access = true", id="unlock"),
    ],
)
async def test_engine_duckdb_reaches_nothing_but_its_database(
    db: Path, kernel: Kernel, tmp_path: Path, sql: str
) -> None:
    out = tmp_path / "leak.csv"
    other = make_db(tmp_path / "other.duckdb")
    provider = FakeConnectionProvider({"shop": FakeConnection(db, read_only=False)})
    broker = broker_for(provider, confirmer=Answers(True))
    kernel.start("r1")
    with pytest.raises(SqlError) as err:
        await run_sql(broker, kernel, sql.format(out=out, other=other))
    assert isinstance(err.value, QueryFailedError), err.value
    assert not out.exists()


# ------------------------------------------------------------------ results in flight


async def test_byte_budget_makes_a_late_caller_wait_then_gives_up() -> None:
    budget = ByteBudget(100, wait_s=0.2)
    await budget.take(80)
    with pytest.raises(SqlUnavailableError):
        await budget.take(40)
    waiter = asyncio.create_task(budget.take(40))
    await asyncio.sleep(0.05)
    await budget.give(80)
    await waiter
    assert budget.available == 60


async def test_the_budget_is_returned_after_each_result(db: Path, kernel: Kernel) -> None:
    broker = broker_for(FakeConnectionProvider({"shop": db}), inflight_bytes=10 * 1024 * 1024)
    kernel.start("r1")
    for _ in range(3):
        await run_sql(broker, kernel, "select * from orders")
    assert broker.budget.available == broker.budget.capacity


async def test_a_failing_statement_is_query_failed(db: Path, kernel: Kernel) -> None:
    kernel.start("r1")
    with pytest.raises(QueryFailedError, match="nope"):
        await run_sql(
            broker_for(FakeConnectionProvider({"shop": db})), kernel, "select * from nope"
        )


async def test_the_session_context_adapter_drives_the_broker(db: Path, tmp_path: Path) -> None:
    from alkera_notebook.sql.broker import RunInfo, SqlKernelContext

    runs = {"r1": RunInfo("r1", Who("person", "alice"), "n.alknb.py")}
    ctx = SqlKernelContext(
        kernel_id="k9",
        workspace=SqlWorkspace(id="w", root="/w"),
        codecs=frozenset({"arrow.ipc.stream"}),
        data_dir=None,
        runs=runs.get,
        row_limit=lambda: 2,
    )
    response = await broker_for(FakeConnectionProvider({"shop": db})).execute(
        ctx, {"sql": "select id from orders", "connection": "shop"}, {"run_id": "r1"}
    )
    assert response["rows"] == 2 and response["truncated"] is True
    runs.clear()
    with pytest.raises(OutsideRunError):
        await broker_for(FakeConnectionProvider({"shop": db})).execute(
            ctx, {"sql": "select 1", "connection": "shop"}, {"run_id": "r1"}
        )
